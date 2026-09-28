"use client";

import { useEffect, useRef, useState } from "react";

import { renderStarfield } from "@/lib/welcome-starfield";

/**
 * 欢迎页（登录 / 初始化）的整屏背景：写实、克制的深空——一片真实感的星空与银河，
 * 画面下方一道行星的地平线，偶尔划过一颗流星。移植自原生 App 的 CosmosBackdrop
 * （apps/apple/MovieClaw/Features/Onboarding/CosmosBackdrop.swift），口径一致：
 *   - 星空位图只画一次（lib/welcome-starfield.ts），整张图绕屏幕中心约 40 分钟转一圈，
 *     旋转是 CSS 动画、由合成线程推进，页面主线程零开销；
 *   - 地平线的大气辉光片头时像轨道日出一样慢慢亮起（lit 之后 1.2 秒起、4.5 秒亮满）；
 *   - 流星 4～9 秒后来第一颗，之后每隔 7～18 秒一颗，飞的那一秒里才逐帧画，平时什么都不画；
 *   - 系统开启「减弱动态效果」时星空不转、也没有流星。
 * 太空里没有闪烁，所以星星不闪；也不画彩色星云和卡通天体。
 */
export function CosmosBackdrop({ lit, dimmed }: { lit: boolean; dimmed: boolean }) {
  const viewport = useViewportSize();
  const reduceMotion = usePrefersReducedMotion();
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 z-0 overflow-hidden bg-black transition-opacity duration-[2600ms] ease-in-out"
      style={{ opacity: lit ? 1 : 0 }}
    >
      {viewport && (
        <>
          <Starfield width={viewport.width} height={viewport.height} spins={!reduceMotion} />
          {/* 画在星空之上、行星之下：划到地平线以下的部分被行星挡住 */}
          {!reduceMotion && <MeteorShower width={viewport.width} height={viewport.height} />}
          <PlanetHorizon width={viewport.width} height={viewport.height} sunrise={lit} />
        </>
      )}
      {/* 表单展开时整体压暗一些，把视线让给输入框 */}
      <div
        className="absolute inset-0 bg-black transition-opacity duration-700"
        style={{ opacity: dimmed ? 0.25 : 0 }}
      />
    </div>
  );
}

/** 视口尺寸（布局视口，不随软键盘变化）；首帧为 null，挂载后再量，避免服务端渲染不一致 */
function useViewportSize(): { width: number; height: number } | null {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    let timer = 0;
    const measure = () => setSize({ width: window.innerWidth, height: window.innerHeight });
    const onResize = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(measure, 150);
    };
    measure();
    window.addEventListener("resize", onResize);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("resize", onResize);
    };
  }, []);
  return size;
}

function usePrefersReducedMotion(): boolean {
  const [reduce, setReduce] = useState(false);
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduce(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduce;
}

/** 位图像素边长上限：桌面大屏按 2 倍画一张对角线见方的图会上百 MB，封顶后星点略柔，正像真实星点的弥散 */
const MAX_STARFIELD_PIXELS = 3200;

/**
 * 星空位图 + 它的缓慢转动。位图边长取视口对角线：旋转到任何角度都铺得满四角。
 * 只在需要更大的图时（窗口放大）才重画；画好后 2 秒淡入。
 */
function Starfield({ width, height, spins }: { width: number; height: number; spins: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [side, setSide] = useState(0);
  const needed = Math.ceil(Math.hypot(width, height));
  useEffect(() => {
    if (needed <= side) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    // 让出首帧（片名等先浮现），再在空闲时画星空
    const handle = window.setTimeout(() => {
      const scale = Math.min(window.devicePixelRatio || 1, 2, MAX_STARFIELD_PIXELS / needed);
      renderStarfield(canvas, needed, scale);
      setSide(needed);
    }, 60);
    return () => window.clearTimeout(handle);
  }, [needed, side]);
  return (
    <canvas
      ref={canvasRef}
      className={`absolute left-1/2 top-1/2 transition-opacity duration-[2000ms] ease-in ${
        spins ? "welcome-starfield-spin" : ""
      }`}
      style={{
        width: side || needed,
        height: side || needed,
        marginLeft: -(side || needed) / 2,
        marginTop: -(side || needed) / 2,
        opacity: side ? 1 : 0,
      }}
    />
  );
}

interface Meteor {
  /** 起点（占视口宽高的比例） */
  startX: number;
  startY: number;
  /** 飞行方向（度；0 向右、90 向下） */
  angle: number;
  /** 飞过的距离（占视口宽的比例） */
  travel: number;
  /** 最长时的尾迹长度（CSS 像素） */
  length: number;
  /** 秒 */
  duration: number;
  /** 亮度 0~1：多数在 0.5~0.8，偶尔一颗很亮 */
  brightness: number;
}

function randomIn(min: number, max: number): number {
  return min + Math.random() * (max - min);
}

function randomMeteor(): Meteor {
  const towardRight = Math.random() < 0.5;
  const bright = Math.random() < 0.15;
  return {
    startX: towardRight ? randomIn(0.05, 0.5) : randomIn(0.5, 0.95),
    startY: randomIn(0.04, 0.38),
    angle: towardRight ? randomIn(22, 40) : randomIn(140, 158),
    travel: randomIn(0.28, 0.48),
    length: randomIn(70, 140),
    duration: randomIn(0.7, 1.2),
    brightness: bright ? 1 : randomIn(0.5, 0.8),
  };
}

/**
 * 流星：一道很细的亮线，头部最亮、尾迹渐隐；点燃后迅速变亮、烧到后段熄灭，一秒左右。
 * 方向斜向下（左下或右下随机），多数暗淡，偶尔一颗亮的带一点光晕。
 * 只有飞的那一秒里逐帧画这一块 canvas，两颗之间什么都不画；页面切到后台时不排新的。
 */
function MeteorShower({ width, height }: { width: number; height: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    let timer = 0;
    let frame = 0;
    let cancelled = false;

    const fly = (meteor: Meteor) => {
      const started = performance.now();
      const radians = (meteor.angle * Math.PI) / 180;
      const dx = Math.cos(radians);
      const dy = Math.sin(radians);
      const draw = (now: number) => {
        if (cancelled) return;
        const linear = Math.min(Math.max((now - started) / 1000 / meteor.duration, 0), 1);
        // 先慢后快：流星冲进大气层时越烧越快
        const progress = linear * linear;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        const distance = meteor.travel * width * progress;
        const hx = meteor.startX * width + dx * distance;
        const hy = meteor.startY * height + dy * distance;
        // 亮度：点燃后迅速变亮，后段慢慢熄灭；尾迹跟着先拉长再收短
        const glow = Math.sin(Math.PI * progress ** 0.7);
        const tail = meteor.length * (0.3 + 0.7 * glow);
        const tx = hx - dx * tail;
        const ty = hy - dy * tail;
        const alpha = meteor.brightness * glow;
        const gradient = ctx.createLinearGradient(tx, ty, hx, hy);
        gradient.addColorStop(0, "rgba(255,255,255,0)");
        gradient.addColorStop(1, `rgba(230,240,255,${0.85 * alpha})`);
        ctx.strokeStyle = gradient;
        ctx.lineWidth = meteor.brightness > 0.9 ? 1.6 : 1.1;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        ctx.lineTo(hx, hy);
        ctx.stroke();
        if (meteor.brightness > 0.9) {
          const halo = ctx.createRadialGradient(hx, hy, 0, hx, hy, 6);
          halo.addColorStop(0, `rgba(217,235,255,${0.35 * alpha})`);
          halo.addColorStop(1, "rgba(217,235,255,0)");
          ctx.fillStyle = halo;
          ctx.fillRect(hx - 6, hy - 6, 12, 12);
        }
        ctx.fillStyle = `rgba(255,255,255,${alpha})`;
        ctx.beginPath();
        ctx.arc(hx, hy, 1.1, 0, Math.PI * 2);
        ctx.fill();
        if (linear < 1) {
          frame = requestAnimationFrame(draw);
        } else {
          ctx.clearRect(0, 0, width, height);
          schedule(randomIn(7, 18));
        }
      };
      frame = requestAnimationFrame(draw);
    };

    const schedule = (seconds: number) => {
      timer = window.setTimeout(() => {
        if (cancelled) return;
        // 后台标签页不放：等回到前台再排下一颗
        if (document.hidden) {
          schedule(randomIn(7, 18));
          return;
        }
        fly(randomMeteor());
      }, seconds * 1000);
    };
    schedule(randomIn(4, 9));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      cancelAnimationFrame(frame);
    };
  }, [width, height]);
  return <canvas ref={canvasRef} className="absolute inset-0 size-full" />;
}

/**
 * 画面下方的行星地平线（夜面朝向我们，太阳正要从右侧升起）。
 *
 * 一颗半径约 2.4 倍视口宽的巨大球体，只露出顶部一段弧；弧上是一线大气层：
 * 细亮线是大气边缘，外面两层模糊的辉光是大气散射，左侧冷蓝、越往右越亮，近太阳处转暖。
 * 整体绕视口中心转 -4°：真实的轨道照片里地平线很少是水平的。
 *
 * 只画看得见的那段弧（-110°～-70°），不画整圆：整圆直径是视口的近五倍，模糊滤镜按
 * 整圆的包围盒分配缓冲会非常吃内存（原生 App 踩过）。可见弧只跨 40°，近乎水平，
 * App 里沿弧的锥形渐变在这里用左右方向的线性渐变等价表达。
 */
function PlanetHorizon({ width: w, height: h, sunrise }: { width: number; height: number; sunrise: boolean }) {
  const r = w * 2.4;
  const cx = w / 2;
  const cy = h * 0.8 + r;
  const at = (deg: number) => {
    const rad = (deg * Math.PI) / 180;
    return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)] as const;
  };
  const [x1, y1] = at(-110);
  const [x2, y2] = at(-70);
  const arc = `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`;
  const body = `${arc} L ${x2} ${h + 400} L ${x1} ${h + 400} Z`;
  // App 的色标铺在 -104°～-70° 这段弧上：换算成水平位置
  const [gx1] = at(-104);
  const [gx2] = at(-70);
  const [dawnX, dawnY] = at(-79);
  return (
    <svg className="absolute inset-0 size-full" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id="cosmos-rim" gradientUnits="userSpaceOnUse" x1={gx1} y1="0" x2={gx2} y2="0">
          <stop offset="0" stopColor="rgb(77,122,217)" stopOpacity="0.2" />
          <stop offset="0.45" stopColor="rgb(107,163,255)" stopOpacity="0.6" />
          <stop offset="0.8" stopColor="rgb(184,217,255)" />
          <stop offset="0.93" stopColor="rgb(255,214,163)" />
          <stop offset="1" stopColor="rgb(255,242,219)" />
        </linearGradient>
        <radialGradient id="cosmos-dawn" gradientUnits="userSpaceOnUse" cx={dawnX} cy={dawnY} r="110">
          <stop offset="0" stopColor="rgb(255,219,178)" stopOpacity="0.16" />
          <stop offset="1" stopColor="rgb(255,219,178)" stopOpacity="0" />
        </radialGradient>
        <filter id="cosmos-blur-wide" x="-50%" y="-200%" width="200%" height="500%">
          <feGaussianBlur stdDeviation="38" />
        </filter>
        <filter id="cosmos-blur-mid" x="-50%" y="-200%" width="200%" height="500%">
          <feGaussianBlur stdDeviation="7" />
        </filter>
        <filter id="cosmos-blur-edge" x="-10%" y="-100%" width="120%" height="300%">
          <feGaussianBlur stdDeviation="0.5" />
        </filter>
        {/* 抹掉落在行星本体上的那半边辉光：行星挡住了身后的大气 */}
        <mask id="cosmos-sky" maskUnits="userSpaceOnUse" x={-w} y={-h} width={w * 3} height={h * 3}>
          <rect x={-w} y={-h} width={w * 3} height={h * 3} fill="white" />
          <path d={body} fill="black" />
        </mask>
      </defs>
      <g transform={`rotate(-4 ${w / 2} ${h / 2})`}>
        {/* 行星夜面：不是纯黑，带一点极暗的蓝，和深空分得开 */}
        <path d={body} fill="rgb(3,4,7)" />
        {/* 大气层：辉光 + 边缘亮线 + 太阳那侧的暖光，日出时整体渐显 */}
        <g
          style={{
            opacity: sunrise ? 1 : 0,
            transition: `opacity 4.5s ease-in-out ${sunrise ? "1.2s" : "0s"}`,
          }}
        >
          <g mask="url(#cosmos-sky)">
            <path d={arc} fill="none" stroke="url(#cosmos-rim)" strokeWidth="56" opacity="0.22" filter="url(#cosmos-blur-wide)" />
            <path d={arc} fill="none" stroke="url(#cosmos-rim)" strokeWidth="10" opacity="0.45" filter="url(#cosmos-blur-mid)" />
          </g>
          <path d={arc} fill="none" stroke="url(#cosmos-rim)" strokeWidth="1.2" filter="url(#cosmos-blur-edge)" />
          <circle cx={dawnX} cy={dawnY} r="110" fill="url(#cosmos-dawn)" />
        </g>
      </g>
    </svg>
  );
}
