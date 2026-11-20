// Vitest 全局 setup：注入 jest-dom 匹配器（toBeInTheDocument 等）
import '@testing-library/jest-dom/vitest'

// jsdom 没有 matchMedia / ResizeObserver，但 antd 的响应式栅格和 Table 要用。
// 不补的话，任何渲染 antd 组件的用例都会挂在 `window.matchMedia is not a function`，
// 而失败信息看起来像是组件写错了——补在这里，别让每个用例各补一遍。
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

// rc-table 量滚动条宽度时会调 getComputedStyle(elt, pseudoElt)，jsdom 对伪元素
// 直接抛 "Not implemented"。丢掉第二个参数即可——量宽度用不到伪元素样式。
const rawGetComputedStyle = window.getComputedStyle.bind(window)
window.getComputedStyle = ((elt: Element) =>
  rawGetComputedStyle(elt)) as typeof window.getComputedStyle

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}
