'use strict';
/** 后台服务（uvicorn）的启动、健康检查和关闭。 */
const path = require('node:path');
const net = require('node:net');
const http = require('node:http');
const { spawn } = require('node:child_process');
const P = require('./paths');
const { append } = require('./log');
const { argsFor } = require('./python');

const IS_WIN = process.platform === 'win32';
const HOST = '127.0.0.1';

/** 探测某个端口上是不是已经跑着一个 TeleOps。 */
function ping(port, timeout = 1000) {
  return new Promise((resolve) => {
    const req = http.get({ host: HOST, port, path: '/api/ping', timeout }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (c) => {
        body += c;
      });
      res.on('end', () => {
        try {
          resolve(JSON.parse(body).ok === true);
        } catch {
          resolve(false);
        }
      });
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

function portFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(false));
    srv.once('listening', () => srv.close(() => resolve(true)));
    srv.listen(port, HOST);
  });
}

/** 让系统分配一个空闲端口。 */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, HOST, () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/**
 * 决定用哪个端口：
 *  - 首选端口空着 → 用它（这样浏览器书签 http://127.0.0.1:8800 一直有效）
 *  - 被别的 TeleOps 占着（比如 start-background.vbs 起的那个）→ 直接接管界面，不再起一份
 *  - 被别的程序占着 → 换一个随机空闲端口
 */
async function choosePort(preferred) {
  if (await portFree(preferred)) return { port: preferred, attach: false };
  if (await ping(preferred)) return { port: preferred, attach: true };
  return { port: await freePort(), attach: false };
}

class Backend {
  constructor() {
    this.child = null;
    this.port = null;
    this.attached = false;
    this.stopping = false;
    this.ready = false;
    this.lines = [];
    this.onExit = null;
  }

  get url() {
    return this.port ? `http://${HOST}:${this.port}/` : null;
  }

  get running() {
    return this.attached || (this.child != null && this.child.exitCode == null);
  }

  tail(n = 60) {
    return this.lines.slice(-n).join('\n');
  }

  /** 起来之后就不再往 desktop.log 里抄后台日志了（teleops.log 里本来就有一份）。 */
  _log(line, persist = !this.ready) {
    this.lines.push(line);
    if (this.lines.length > 400) this.lines.splice(0, this.lines.length - 400);
    if (persist) append(line);
  }

  /** 启动（或接管）后台服务，直到 /api/ping 通了才 resolve。 */
  async start({ python, port, onLog }) {
    const say = (line) => {
      this._log(line);
      if (onLog) onLog(line);
    };

    this.ready = false;
    const chosen = await choosePort(port);
    this.port = chosen.port;
    if (chosen.attach) {
      this.attached = true;
      say(`检测到已经在运行的 TeleOps，直接接管 ${this.url}`);
      this.ready = true;
      return this.url;
    }

    const args = argsFor(python, [
      path.join(P.pySrc, 'run.py'),
      '--host',
      HOST,
      '--port',
      String(this.port),
    ]);
    say(`启动后台：${python} ${args.join(' ')}`);
    say(`数据目录：${P.home}`);

    this.child = spawn(python, args, {
      cwd: P.pySrc,
      env: {
        ...process.env,
        TELEOPS_HOME: P.home,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
      },
      windowsHide: true,
      detached: !IS_WIN, // 非 Windows 单独开进程组，方便整组结束
    });

    let exited = null;
    const grab = (buf) => {
      for (const line of buf.toString().split(/\r?\n/)) {
        if (line.trim()) say(line.trimEnd());
      }
    };
    this.child.stdout.on('data', grab);
    this.child.stderr.on('data', grab);
    this.child.on('error', (err) => {
      exited = `无法启动 Python：${err.message}`;
    });
    this.child.on('exit', (code, signal) => {
      exited = `后台进程已退出（code=${code}, signal=${signal}）`;
      this.ready = false;
      this._log(exited, true);
      const child = this.child;
      this.child = null;
      if (!this.stopping && this.onExit) this.onExit(exited, this.tail(), child);
    });

    const deadline = Date.now() + 180000; // 首次运行要建库，给足时间
    while (Date.now() < deadline) {
      if (exited) throw new Error(`${exited}\n\n${this.tail(30)}`);
      if (await ping(this.port, 800)) {
        say(`后台就绪 → ${this.url}`);
        this.ready = true;
        return this.url;
      }
      await new Promise((r) => setTimeout(r, 400));
    }
    await this.stop();
    throw new Error(`后台启动超时（3 分钟）。\n\n${this.tail(30)}`);
  }

  /** 结束后台进程（含子进程），最多等 8 秒。 */
  async stop() {
    this.stopping = true;
    const child = this.child;
    this.child = null;
    this.attached = false;
    if (!child || child.exitCode != null) {
      this.stopping = false;
      return;
    }

    const dead = new Promise((resolve) => child.once('exit', resolve));
    try {
      if (IS_WIN) {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
      } else {
        process.kill(-child.pid, 'SIGTERM');
      }
    } catch {
      try {
        child.kill();
      } catch {
        /* 已经没了 */
      }
    }

    const timer = new Promise((r) => setTimeout(r, 8000));
    await Promise.race([dead, timer]);
    if (child.exitCode == null && !IS_WIN) {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch {
        /* 已经没了 */
      }
    }
    this.stopping = false;
  }
}

module.exports = { Backend, ping, choosePort };
