import Particles, { ParticlesProvider } from '@tsparticles/react'
import { loadSlim } from '@tsparticles/slim'
import type { Engine, ISourceOptions } from '@tsparticles/engine'

// 注意：@tsparticles/react v4 要求 init 是「稳定引用」，必须定义在模块级（不能是内联箭头函数），
// 否则 ParticlesProvider 每次渲染对比到新引用会抛 "init callback must be stable" 导致整页崩溃。
const initParticles = async (engine: Engine): Promise<void> => {
  await loadSlim(engine)
}

// 问答页动态科技背景：含蓄版电光粒子 + 三团流动极光光晕（铺满但不挡交互，内容在上层滚动）
const options: ISourceOptions = {
  fullScreen: { enable: false },
  background: { color: 'transparent' },
  fpsLimit: 60,
  detectRetina: true,
  particles: {
    // 比登录页更稀更淡，避免干扰阅读回答
    number: { value: 36, density: { enable: true, width: 1280, height: 800 } },
    color: { value: '#00c6ff' },
    links: {
      enable: true,
      color: '#00c6ff',
      opacity: 0.12,
      distance: 150,
      width: 1,
    },
    move: { enable: true, speed: 0.4, outModes: { default: 'out' } },
    size: { value: { min: 1, max: 2 } },
    opacity: { value: 0.35 },
  },
  // 后台不加 hover 交互，专注漂浮氛围
  interactivity: { events: { onHover: { enable: false } } },
}

export default function ChatBackground() {
  return (
    <div className="chat-bg" aria-hidden>
      {/* 三团极光光晕：CSS 动画缓慢漂移 */}
      <div className="aurora a1" />
      <div className="aurora a2" />
      <div className="aurora a3" />
      {/* 粒子层 */}
      <div className="chat-particles">
        <ParticlesProvider init={initParticles}>
          <Particles id="chat-tsparticles" options={options} style={{ width: '100%', height: '100%' }} />
        </ParticlesProvider>
      </div>
    </div>
  )
}
