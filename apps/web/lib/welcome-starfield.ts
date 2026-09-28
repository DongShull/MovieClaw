/**
 * 欢迎页（登录 / 初始化）的星空位图：把整片星空一次性画进一块 canvas。
 *
 * 一比一移植原生 App 的 StarfieldRenderer（apps/apple/MovieClaw/Features/Onboarding/
 * CosmosBackdrop.swift）：写实、克制，靠「少」和「按物理来」——
 *   1. 银河的弥散光：沿星带铺几百团极淡的柔光，亮度由分形噪声调制成一块块星云，再被尘埃带挖暗；
 *   2. 银河的星点：几万颗极暗的小星，同样按噪声与尘埃带取舍——远看就是一条有纹理、有暗缝的光带；
 *   3. 前景恒星：一千多颗，亮度按幂律分布（暗星极多、亮星极少），颜色按色温取，亮星加一圈光晕。
 * 随机数用固定种子，每次打开都是同一片天。星星不闪烁（太空里没有大气抖动）。
 *
 * 坐标单位是 CSS 像素，画布按 scale 放大绘制；位图边长取视口对角线，
 * 整张图绕中心缓慢旋转时转到任何角度都铺满四角（旋转交给 CSS 动画，见 cosmos-backdrop.tsx）。
 */

/** 固定种子的伪随机数（mulberry32）：同一个种子永远生成同一片星空 */
class SeededRandom {
  private state: number;

  constructor(seed: number) {
    this.state = seed >>> 0;
  }

  /** 0~1 均匀分布 */
  next(): number {
    this.state = (this.state + 0x6d2b79f5) >>> 0;
    let t = this.state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  /** 标准正态分布（Box-Muller） */
  gaussian(): number {
    const u = Math.max(this.next(), 1e-12);
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * this.next());
  }
}

/** 值噪声的格点哈希：整数格点 → 0~1 */
function hash(x: number, y: number, seed: number): number {
  let h = (Math.imul(x, 374761393) + Math.imul(y, 668265263) + Math.imul(seed, 1274126177)) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h & 0xffffff) / 0xffffff;
}

function valueNoise(x: number, y: number, seed: number): number {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const fx = x - xi;
  const fy = y - yi;
  const ux = fx * fx * (3 - 2 * fx);
  const uy = fy * fy * (3 - 2 * fy);
  const a = hash(xi, yi, seed);
  const b = hash(xi + 1, yi, seed);
  const c = hash(xi, yi + 1, seed);
  const d = hash(xi + 1, yi + 1, seed);
  return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy;
}

/** 值噪声 + 五层分形叠加：给银河加上一块块的星云纹理与尘埃暗缝 */
function fbm(x: number, y: number, seed: number): number {
  let total = 0;
  let amplitude = 0.5;
  let frequency = 1;
  let norm = 0;
  for (let octave = 0; octave < 5; octave++) {
    total += amplitude * valueNoise(x * frequency, y * frequency, seed + octave * 131);
    norm += amplitude;
    amplitude *= 0.5;
    frequency *= 2;
  }
  return total / norm;
}

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.min(Math.max((x - edge0) / (edge1 - edge0), 0), 1);
  return t * t * (3 - 2 * t);
}

/** 银河星带的几何：一条斜穿画面的直线，星带宽度按高斯分布衰减；一端是更亮的银心方向 */
class Band {
  readonly ox: number;
  readonly oy: number;
  readonly ax: number;
  readonly ay: number;
  readonly nx: number;
  readonly ny: number;
  /** 星带的半宽（高斯分布的 σ） */
  readonly sigma: number;

  constructor(readonly side: number) {
    this.ox = side * 0.5;
    this.oy = side * 0.44;
    const angle = (58 * Math.PI) / 180;
    this.ax = Math.cos(angle);
    this.ay = Math.sin(angle);
    this.nx = -Math.sin(angle);
    this.ny = Math.cos(angle);
    this.sigma = side * 0.085;
  }

  point(along: number, offset: number): [number, number] {
    return [this.ox + this.ax * along + this.nx * offset, this.oy + this.ay * along + this.ny * offset];
  }

  /** 某点的银河亮度（0~1）：星带高斯衰减 × 银心方向渐强 × 星云纹理 × 尘埃带 */
  density(x: number, y: number): number {
    const dx = x - this.ox;
    const dy = y - this.oy;
    const across = (dx * this.nx + dy * this.ny) / this.sigma;
    const lengthwise = (dx * this.ax + dy * this.ay) / this.side;
    const profile = Math.exp(-across * across);
    const core = 0.45 + 0.55 * Math.exp(-(((lengthwise + 0.18) / 0.32) ** 2));
    const clouds = smoothstep(0.32, 0.78, fbm(x / 95, y / 95, 11));
    // 尘埃带：星带中线附近被一条条暗缝切开
    const dust = smoothstep(0.5, 0.66, fbm(x / 42, y / 42, 29)) * Math.exp(-((across / 0.55) ** 2));
    return profile * core * clouds * (1 - 0.85 * dust);
  }
}

/** 恒星颜色按色温抽样：蓝白（O/B/A 型）、白、淡黄（G 型，像太阳）、橙（K/M 型），都压得很淡 */
function temperatureColor(u: number): [number, number, number] {
  if (u < 0.22) return [204, 222, 255];
  if (u < 0.68) return [255, 255, 255];
  if (u < 0.9) return [255, 242, 217];
  return [255, 214, 168];
}

function dot(ctx: CanvasRenderingContext2D, x: number, y: number, r: number) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fill();
}

function softGlow(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  radius: number,
  rgb: [number, number, number],
  alpha: number,
) {
  const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius);
  gradient.addColorStop(0, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha})`);
  gradient.addColorStop(1, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`);
  ctx.fillStyle = gradient;
  ctx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
}

/**
 * 把整片星空画进 canvas（边长 side CSS 像素、按 scale 倍绘制）。
 * 手机上约一两百毫秒，调用方放在首帧之后执行，画好再淡入。
 */
export function renderStarfield(canvas: HTMLCanvasElement, side: number, scale: number): void {
  const pixels = Math.round(side * scale);
  canvas.width = pixels;
  canvas.height = pixels;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.scale(scale, scale);

  const rng = new SeededRandom(0x4d6f7669); // "Movi"
  const band = new Band(side);

  // 1. 银河弥散光
  for (let i = 0; i < 360; i++) {
    const [x, y] = band.point((rng.next() - 0.5) * side * 1.5, rng.gaussian() * band.sigma);
    const strength = band.density(x, y);
    if (strength <= 0.02) continue;
    const radius = 16 + 34 * rng.next();
    softGlow(ctx, x, y, radius, [237, 235, 230], 0.045 * strength);
  }

  // 2. 银河星点：银心附近偏暖、外侧偏冷
  for (let i = 0; i < 70000; i++) {
    const [x, y] = band.point((rng.next() - 0.5) * side * 1.5, rng.gaussian() * band.sigma * 1.2);
    if (rng.next() >= band.density(x, y)) continue;
    const alpha = 0.05 + 0.3 * rng.next() ** 2;
    const radius = 0.28 + 0.25 * rng.next();
    const warm = rng.next() < 0.5;
    ctx.fillStyle = warm ? `rgba(255,242,224,${alpha})` : `rgba(224,235,255,${alpha})`;
    dot(ctx, x, y, radius);
  }

  // 3. 前景恒星：前 1100 颗均匀撒满全天，后 600 颗沿星带加密（银河方向本来星就多）
  for (let index = 0; index < 1700; index++) {
    const [x, y] =
      index < 1100
        ? [rng.next() * side, rng.next() * side]
        : band.point((rng.next() - 0.5) * side * 1.5, rng.gaussian() * band.sigma * 1.6);
    // 亮度按幂律：u⁷ 让绝大多数星都很暗，显眼的亮星只有几十颗（与肉眼看到的星空比例相当）
    const brightness = rng.next() ** (index < 1100 ? 7 : 9);
    const radius = 0.26 + 0.75 * brightness ** 0.8;
    const alpha = 0.16 + 0.84 * brightness ** 0.55;
    const color = temperatureColor(rng.next());
    if (brightness > 0.55) {
      // 只有最亮的几十颗带光晕：光学系统里的弥散，很淡、很小，不是卡通的光圈
      softGlow(ctx, x, y, radius * 2.6 + 3 * brightness, color, 0.16 * brightness);
    }
    ctx.fillStyle = `rgba(${color[0]},${color[1]},${color[2]},${alpha})`;
    dot(ctx, x, y, radius);
  }
}
