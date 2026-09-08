/* 动效层：GSAP 动画全部集中在这里，页面代码只调具名函数。

   三条约束，写在最前面免得后来人踩：

   1. 动画只在**状态真的变了**的时候播。概览页每 20 秒 render() 一次、画布每 8 秒
      刷执行记录，把入场动画挂在渲染上会变成每 20 秒闪一次。所以入场动画由调用方
      传 changed 标记，数字滚动只在数值和上次不同时才滚。
   2. 只动 transform 和 opacity。宽高、top/left 会触发布局，一次重排就把 60fps 吃没了。
      画布上的节点是例外——那是 SVG 的 transform 属性，见 tidyNodes 的注释。
   3. 系统开了「减少动态效果」就一律瞬时生效。前庭功能障碍的人看动画会眩晕，
      这不是可选项。GSAP 没加载出来时同理降级，页面不能因为少个动画库就不能用。 */
(() => {
'use strict';

const g = window.gsap;
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
// 这里不用 gsap.matchMedia()：它是给"按条件建立、条件不成立时自动回滚"的
// 常驻动画用的，而这里全是一次性的事件动画（弹一个 toast、开一个弹窗），
// 建完就结束，没有需要回滚的东西。
// 还要求页面当前是可见的。标签页在后台时浏览器不给动画帧，补间不推进，
// 而 from() 已经把元素设成 opacity 0 了——结果是后台打开这个页面，回来一看
// 半张页面是空的。隐藏时一律走瞬时分支：不动画，但内容一定在。
const on = () => !!g && !reduce.matches && !document.hidden;

if (g) {
  g.defaults({ duration: 0.4, ease: 'power2.out' });
  // 后台页面不必空转
  g.ticker.lagSmoothing(500, 33);
}

/** 立刻执行 fn，用于降级路径。 */
const now = (fn) => { if (typeof fn === 'function') fn(); };

const A = {
  enabled: on,

  // ─────────────────────────────────────────────── 页面
  /** 换页时让内容错落地落进来。changed=false（定时刷新）时什么都不做。 */
  pageEnter(view, changed) {
    if (!changed || !on() || !view) return;
    const blocks = [...view.children];
    if (!blocks.length) return;
    g.from(blocks, {
      y: 12, autoAlpha: 0, duration: 0.38,
      stagger: { each: 0.045, from: 'start' },
      // 动完把内联样式清掉，免得留下 visibility/opacity 影响后续的类名切换
      clearProps: 'transform,opacity,visibility',
    });
  },

  /** 统计数字滚动。只在数值和上次不同时才滚——每次刷新都从 0 滚起是噪音。 */
  countUp(el, value, key) {
    if (!el) return;
    const prev = A._counts.get(key);
    A._counts.set(key, value);
    if (!on() || prev === undefined || prev === value) return;
    const box = { v: prev };
    g.to(box, {
      v: value, duration: 0.7, ease: 'power2.out',
      onUpdate: () => { el.textContent = Math.round(box.v); },
      onComplete: () => { el.textContent = value; },
    });
    // 变大了顺手提一下，眼睛容易注意到
    if (value > prev) g.fromTo(el, { color: 'var(--accent)' }, { color: '', duration: 1.1, ease: 'power1.out' });
  },
  _counts: new Map(),

  // ─────────────────────────────────────────────── 提示条
  toastIn(el) {
    if (!on()) return;
    g.from(el, { autoAlpha: 0, x: 28, scale: 0.96, duration: 0.32, ease: 'power3.out' });
  },
  /** 淡出并把自己的高度收掉，下面的 toast 才不会往上跳一格。 */
  toastOut(el, done) {
    if (!on()) return now(done);
    g.to(el, {
      autoAlpha: 0, x: 28, duration: 0.22, ease: 'power2.in',
      onComplete: () => {
        // 高度收拢是布局动画，但只在消失这一下、且最多两三个元素，值这个钱：
        // 不收的话剩下的 toast 会瞬间跳位，比动画本身更晃眼
        g.to(el, {
          height: 0, marginTop: 0, paddingTop: 0, paddingBottom: 0,
          duration: 0.18, ease: 'power2.inOut', onComplete: () => now(done),
        });
      },
    });
  },

  // ─────────────────────────────────────────────── 弹窗
  modalIn(layer) {
    if (!on()) return;
    const box = layer.querySelector('.modal');
    g.from(layer.querySelector('.mask'), { autoAlpha: 0, duration: 0.2 });
    g.from(box, { y: 14, scale: 0.97, autoAlpha: 0, duration: 0.28, ease: 'back.out(1.4)' });
  },
  /** 关闭动画期间弹窗还在 DOM 里，先让它不吃点击，否则会挡住底下的按钮。 */
  modalOut(layer, done) {
    if (!on()) return now(done);
    layer.style.pointerEvents = 'none';
    g.to(layer.querySelector('.modal'), { y: 8, scale: 0.98, autoAlpha: 0, duration: 0.16, ease: 'power2.in' });
    g.to(layer.querySelector('.mask'), { autoAlpha: 0, duration: 0.18, onComplete: () => now(done) });
  },

  // ─────────────────────────────────────────────── 画布
  /** 「规整」：让节点滑到新位置，而不是瞬移。
   *
   *  动的是节点数据对象的 x/y（普通 JS 数字），每帧回调让 graph.js 自己去写
   *  SVG 的 transform 和重算连线。绝不让 GSAP 直接碰那个 <g> 的 transform 属性
   *  ——拖动和重绘都在写它，两边抢同一个属性必然打架。
   */
  tidyNodes(nodes, target, onFrame, done) {
    const apply = () => nodes.forEach((n) => {
      const t = target.get(n.node_id);
      if (t) { n.x = t.x; n.y = t.y; }
    });
    if (!on()) { apply(); now(onFrame); return now(done); }
    const moving = nodes.filter((n) => {
      const t = target.get(n.node_id);
      return t && (Math.abs(t.x - n.x) > 0.5 || Math.abs(t.y - n.y) > 0.5);
    });
    if (!moving.length) { apply(); now(onFrame); return now(done); }
    g.to(moving, {
      x: (i, n) => target.get(n.node_id).x,
      y: (i, n) => target.get(n.node_id).y,
      duration: 0.6, ease: 'power3.inOut',
      stagger: { amount: 0.22, from: 'start' },
      onUpdate: onFrame,
      onComplete: () => { apply(); now(onFrame); now(done); },
    });
  },

  /** 选中节点时轻轻脉冲一下选中框。动的是描边，不碰节点的 transform。 */
  pulse(el) {
    if (!on() || !el) return;
    const rect = el.querySelector('.body');
    if (!rect) return;
    g.fromTo(rect, { strokeWidth: 3.4 }, { strokeWidth: '', duration: 0.45, ease: 'power2.out', clearProps: 'strokeWidth' });
  },

  /** 有未保存的改动时，提示文字冒一下头，别让它无声无息地出现。 */
  flag(el) {
    if (!on() || !el || !el.textContent) return;
    g.fromTo(el, { autoAlpha: 0, y: -4 }, { autoAlpha: 1, y: 0, duration: 0.25, clearProps: 'transform,opacity,visibility' });
  },
};

window.TeleOpsAnim = A;
})();
