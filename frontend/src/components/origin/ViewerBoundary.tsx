/**
 * 阅读器的错误边界。
 *
 * 为什么单独包一层：阅读器是**懒加载的第三方渲染器**（pdf.js + canvas），
 * 一旦它抛错，React 会把整棵子树卸掉——表现就是"点开原文，界面黑屏、什么都没有"。
 * 有了边界，最坏情况也只是阅读器区域显示一句可读的错误，页面其余部分不受影响。
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Alert } from 'antd'

interface Props {
  children: ReactNode
}

interface State {
  error: string | null
}

export default class ViewerBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error.message : String(error) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 控制台留全量栈，方便用户截图报障
    console.error('[PdfOriginViewer] 渲染崩溃', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <Alert
          type="error"
          showIcon
          style={{ margin: 16 }}
          message="原文阅读器崩溃了"
          description={`${this.state.error}（可以关掉重开一次；反复出现请把这条错误发给管理员）`}
        />
      )
    }
    return this.props.children
  }
}
