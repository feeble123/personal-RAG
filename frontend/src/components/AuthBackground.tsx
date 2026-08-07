import Particles, { ParticlesProvider } from '@tsparticles/react'
import { loadSlim } from '@tsparticles/slim'
import type { Engine, ISourceOptions } from '@tsparticles/engine'

// init 必须为稳定引用（模块级定义），否则 ParticlesProvider 会在重渲染时抛异常导致整页崩溃
const initParticles = async (engine: Engine): Promise<void> => {
  await loadSlim(engine)
}

// 深色科技风粒子背景：电光青蓝粒子 + 连线，hover 聚拢
const options: ISourceOptions = {
  fullScreen: { enable: false },
  background: { color: 'transparent' },
  fpsLimit: 60,
  detectRetina: true,
  particles: {
    number: { value: 70, density: { enable: true, width: 1280, height: 800 } },
    color: { value: '#00c6ff' },
    links: {
      enable: true,
      color: '#00c6ff',
      opacity: 0.22,
      distance: 140,
      width: 1,
    },
    move: { enable: true, speed: 0.5, outModes: { default: 'out' } },
    size: { value: { min: 1, max: 2.2 } },
    opacity: { value: 0.55 },
  },
  interactivity: {
    events: { onHover: { enable: true, mode: 'grab' } },
    modes: { grab: { distance: 160, links: { opacity: 0.4 } } },
  },
}

export default function AuthBackground() {
  return (
    <ParticlesProvider init={initParticles}>
      <div className="auth-particles" aria-hidden>
        <Particles id="auth-tsparticles" options={options} style={{ width: '100%', height: '100%' }} />
      </div>
    </ParticlesProvider>
  )
}
