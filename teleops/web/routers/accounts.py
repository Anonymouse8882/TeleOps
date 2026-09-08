"""账号管理 + 登录流程。"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from telethon.sessions import SQLiteSession

from ...db.models import Account, Channel
from ...tg.client import inspect_session_file, string_session
from ..deps import DbDep, EngineDep, SettingsDep, ok
from ..schemas import AccountIn, AccountPatch, CodeIn, CodeWatchIn, PasswordIn

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
log = logging.getLogger(__name__)


def _dump(a: Account, engine) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "api_id": engine.tg_api.api_id or a.api_id,
        # 全局值一旦配好就优先于账号自带的，这里如实反映真正生效的来源
        "uses_global_api": bool(engine.tg_api.configured),
        "phone": a.phone,
        "is_bot": a.is_bot,
        "proxy": a.proxy,
        "enabled": a.enabled,
        "status": a.status,
        "me": a.me or {},
        "connected": engine.clients.is_connected(a.id),
        "login_stage": engine.clients.login_stage(a.id),
        "code_watch": engine.code_watcher.status(a.id),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("")
async def list_accounts(db: DbDep, engine: EngineDep):
    rows = (await db.execute(select(Account).order_by(Account.id))).scalars().all()
    return ok([_dump(a, engine) for a in rows])


@router.post("")
async def create_account(payload: AccountIn, db: DbDep, engine: EngineDep):
    exists = (await db.execute(select(Account).where(Account.name == payload.name))).first()
    if exists:
        raise HTTPException(400, "账号名称已存在")
    _require_global_api(engine)

    data = payload.model_dump()
    # 凭据统一走全局设置，账号上不再各存一份
    data["api_id"] = 0
    data["api_hash"] = ""
    acc = Account(**data)
    db.add(acc)
    await db.flush()
    return ok(_dump(acc, engine))


@router.patch("/{account_id}")
async def update_account(account_id: int, payload: AccountPatch, db: DbDep, engine: EngineDep):
    acc = await db.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(acc, k, v)
    await db.flush()
    await engine.clients.close(account_id)  # 配置变了，重建连接
    return ok(_dump(acc, engine))


@router.delete("/{account_id}")
async def delete_account(account_id: int, db: DbDep, engine: EngineDep):
    acc = await db.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    used = (await db.execute(select(Channel.id).where(Channel.account_id == account_id))).first()
    if used:
        raise HTTPException(400, "仍有频道绑定在该账号上，请先解绑")
    await engine.code_watcher.stop(account_id)
    await engine.clients.close(account_id)
    await db.delete(acc)
    return ok()


# ------------------------------------------------------------- 登录验证码监听
@router.post("/{account_id}/code-watch")
async def start_code_watch(
    account_id: int, payload: CodeWatchIn, db: DbDep, engine: EngineDep
):
    """开始监听该账号收到的登录验证码。

    适用场景：这个号已经登录在 TeleOps 里，你要在别的设备/程序上登录它。
    Telegram 会把验证码发给账号自己，这里替你读出来。
    """
    acc = await _need(db, account_id)
    if acc.is_bot:
        raise HTTPException(400, "Bot 账号收不到登录验证码")
    if acc.status != "online":
        raise HTTPException(400, "该账号未登录，无法读取它收到的消息。请先登录或导入 session。")
    try:
        return ok(await engine.code_watcher.start(acc, payload.seconds))
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/{account_id}/code-watch")
async def poll_code_watch(account_id: int, engine: EngineDep):
    """轮询监听结果。"""
    return ok(engine.code_watcher.status(account_id))


@router.delete("/{account_id}/code-watch")
async def stop_code_watch(account_id: int, engine: EngineDep):
    await engine.code_watcher.stop(account_id)
    return ok()


# ----------------------------------------------------------------- 导入
#: 正常的 .session 只有几十 KB，给足余量即可
MAX_SESSION_BYTES = 8 * 1024 * 1024


@router.post("/import")
async def import_session(
    db: DbDep,
    engine: EngineDep,
    name: str = Form(...),
    proxy: str = Form(""),
    session_string: str = Form(""),
    enabled: bool = Form(True),
    file: UploadFile | None = File(None),
    meta: UploadFile | None = File(None),
):
    """用现成的 Telethon session 直接导入一个已登录账号。

    两种来源二选一：上传 .session 文件，或粘贴 session string。
    可附带账号商常给的同名 .json，用来自动填 api_id / api_hash。
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "请填写账号名称")
    if (await db.execute(select(Account.id).where(Account.name == name))).first():
        raise HTTPException(400, "账号名称已存在")

    _require_global_api(engine)
    api_id, api_hash = engine.tg_api.api_id, engine.tg_api.api_hash

    # 账号包附带的 json 里记着这份 session 是用哪套 api 生成的，
    # 和全局值对不上就直接说清楚，别让用户对着"密钥无效"猜半天
    if meta is not None and meta.filename:
        _warn_meta_mismatch(await meta.read(), api_id)

    tmp_dir = engine.settings.storage.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None

    # ---- 组装 session ----
    if file is not None and file.filename:
        raw = await file.read()
        if not raw:
            raise HTTPException(400, "上传的文件是空的")
        if len(raw) > MAX_SESSION_BYTES:
            raise HTTPException(400, "文件太大了，正常的 .session 只有几十 KB")

        tmp_path = tmp_dir / f"import_{uuid.uuid4().hex}.session"
        tmp_path.write_bytes(raw)

        kind, detail = inspect_session_file(tmp_path)
        if kind == "pyrogram":
            _cleanup(tmp_path)
            raise HTTPException(400, f"{detail}，请改用 Telethon 生成的 session。")
        if kind != "telethon":
            _cleanup(tmp_path)
            raise HTTPException(400, f"这不是可用的 Telethon session：{detail}")
        session: Any = SQLiteSession(str(tmp_path))
    elif session_string.strip():
        try:
            session = string_session(session_string)
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        raise HTTPException(400, "请上传 .session 文件或粘贴 session string")

    # ---- 连一次，确认真的处于已登录状态 ----
    try:
        me = await engine.clients.probe_session(session, api_id, api_hash, proxy)
    except Exception as e:
        _discard(session, tmp_path)
        raise HTTPException(400, str(e))

    # ---- 落库 ----
    acc = Account(
        name=name,
        api_id=api_id,
        api_hash=api_hash,
        phone=str(me.get("phone") or ""),
        is_bot=bool(me.get("bot")),
        proxy=proxy.strip(),
        enabled=enabled,
        status="online",
        me=me,
    )
    db.add(acc)
    await db.flush()

    # ---- session 落位 ----
    try:
        if tmp_path is not None:
            _install_session(tmp_path, engine.settings.sessions_dir, acc.session_name)
        else:
            # session string 没有文件，转存成本地 SQLite session 供后续复用
            _persist_string_session(session, engine.settings.sessions_dir, acc.session_name)
    except Exception as e:
        _discard(session, tmp_path)
        raise HTTPException(500, f"保存 session 失败：{e}")

    log.info("已通过 session 导入账号「%s」（%s）", name, me.get("username") or me.get("id"))
    return ok({**_dump(acc, engine), "me": me})


@router.post("/inspect-session")
async def inspect_uploaded(settings: SettingsDep, file: UploadFile = File(...)):
    """只看文件格式，不联网；上传后立刻给用户一个反馈。"""
    raw = await file.read()
    if len(raw) > MAX_SESSION_BYTES:
        raise HTTPException(400, "文件太大了，正常的 .session 只有几十 KB")

    tmp_dir = settings.storage.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"peek_{uuid.uuid4().hex}.session"
    tmp.write_bytes(raw)
    try:
        kind, detail = inspect_session_file(tmp)
    finally:
        _cleanup(tmp)
    return ok({"kind": kind, "detail": detail, "usable": kind == "telethon"})


def _require_global_api(engine) -> None:
    if not engine.tg_api.configured:
        raise HTTPException(
            400,
            "还没有配置 Telegram API。请先到「设置」页填写 api_id / api_hash，"
            "所有账号共用这一套。",
        )


def _warn_meta_mismatch(raw: bytes, api_id: int) -> None:
    """账号包 json 里的 app_id 和全局设置不一致时直接拦下。

    session 的密钥绑定在生成它的那套 api 上，用别的 api 连必定失败；
    与其让 Telegram 回一句"密钥无效"，不如提前说明白。
    """
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise HTTPException(400, "附带的 json 解析失败")
    if not isinstance(data, dict):
        raise HTTPException(400, "附带的 json 格式不对，应该是一个对象")

    for key in ("app_id", "api_id", "appId", "apiId"):
        if data.get(key):
            try:
                theirs = int(data[key])
            except (TypeError, ValueError):
                return
            if theirs != api_id:
                raise HTTPException(
                    400,
                    f"这份 session 是用 api_id {theirs} 生成的，"
                    f"和「设置」里的全局 api_id {api_id} 不一致，导入必定失败。"
                    "请先把设置里的 api 改成 {0}，或换一份用当前 api 生成的 session。".format(theirs),
                )
            return


def _install_session(tmp_path: Path, sessions_dir: Path, session_name: str) -> None:
    """把临时 session 挪到正式位置，顺带清掉可能残留的旁路文件。"""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        stale = sessions_dir / f"{session_name}.session{suffix}"
        if stale.exists():
            stale.unlink()
    shutil.move(str(tmp_path), str(sessions_dir / f"{session_name}.session"))
    for suffix in ("-journal", "-wal", "-shm"):
        side = Path(str(tmp_path) + suffix)
        if side.exists():
            side.unlink()


def _persist_string_session(session: Any, sessions_dir: Path, session_name: str) -> None:
    """把 StringSession 的密钥写进本地 SQLite session，之后就和文件导入一样了。"""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    dest = sessions_dir / session_name
    for suffix in (".session", ".session-journal"):
        stale = Path(str(dest) + suffix)
        if stale.exists():
            stale.unlink()

    sq = SQLiteSession(str(dest))
    try:
        sq.set_dc(session.dc_id, session.server_address, session.port)
        sq.auth_key = session.auth_key
        sq.save()
    finally:
        sq.close()


def _discard(session: Any, path: Path | None) -> None:
    """放弃这次导入：先释放 SQLite 句柄，再删临时文件。

    顺序不能反——Windows 上只要连接还开着，文件就删不掉。
    """
    try:
        session.close()
    except Exception:
        pass
    _cleanup(path)


def _cleanup(path: Path | None) -> None:
    if path is None:
        return
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        try:
            if p.exists():
                p.unlink()
        except OSError as e:
            # 删不掉就留给启动时的清扫兜底，但别默默吞掉
            log.warning("临时 session 文件清理失败 %s：%s", p.name, e)


# ------------------------------------------------------------------- 登录
@router.post("/{account_id}/login")
async def login(account_id: int, db: DbDep, engine: EngineDep):
    acc = await _need(db, account_id)
    try:
        res = await engine.clients.start_login(acc)
    except Exception as e:
        acc.status = "error"
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    if res.get("stage") == "done":
        acc.status = "online"
        acc.me = res.get("me") or {}
    else:
        acc.status = "pending_" + res["stage"]
    await db.flush()
    return ok(res)


@router.post("/{account_id}/code")
async def submit_code(account_id: int, payload: CodeIn, db: DbDep, engine: EngineDep):
    acc = await _need(db, account_id)
    try:
        res = await engine.clients.submit_code(acc, payload.code)
    except Exception as e:
        raise HTTPException(400, str(e))
    if res.get("stage") == "done":
        acc.status = "online"
        acc.me = res.get("me") or {}
    else:
        acc.status = "pending_password"
    await db.flush()
    return ok(res)


@router.post("/{account_id}/password")
async def submit_password(account_id: int, payload: PasswordIn, db: DbDep, engine: EngineDep):
    acc = await _need(db, account_id)
    try:
        res = await engine.clients.submit_password(acc, payload.password)
    except Exception as e:
        raise HTTPException(400, str(e))
    acc.status = "online"
    acc.me = res.get("me") or {}
    await db.flush()
    return ok(res)


@router.post("/{account_id}/logout")
async def logout(account_id: int, db: DbDep, engine: EngineDep):
    acc = await _need(db, account_id)
    await engine.code_watcher.stop(account_id)
    try:
        await engine.clients.logout(acc)
    except Exception as e:
        log.warning("退出登录失败：%s", e)
    acc.status = "logged_out"
    acc.me = {}
    await db.flush()
    return ok()


@router.get("/{account_id}/dialogs")
async def dialogs(account_id: int, db: DbDep, engine: EngineDep, limit: int = 200):
    """列出该账号可见的频道/群组，用于频道管理里的快速导入。"""
    acc = await _need(db, account_id)
    try:
        client = await engine.clients.get(acc)
    except Exception as e:
        raise HTTPException(400, str(e))

    out: list[dict[str, Any]] = []
    async for d in client.iter_dialogs(limit=limit):
        if not (d.is_channel or d.is_group):
            continue
        entity = d.entity
        out.append(
            {
                "id": d.id,
                "title": d.title or "",
                "username": getattr(entity, "username", None),
                "peer": ("@" + entity.username) if getattr(entity, "username", None) else str(d.id),
                "kind": "channel" if d.is_channel and not d.is_group else "group",
                "broadcast": bool(getattr(entity, "broadcast", False)),
                "admin": bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None)),
                "participants": getattr(entity, "participants_count", None),
            }
        )
    out.sort(key=lambda x: (not x["admin"], x["title"]))
    return ok(out)


async def _need(db, account_id: int) -> Account:
    acc = await db.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    return acc
