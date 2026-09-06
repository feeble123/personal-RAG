import { useRef, useState } from 'react'
import { ArrowUpOutlined, StopOutlined } from '@ant-design/icons'

// 输入框（裁剪自 Agent 组件库 PromptInput，改透明青蓝）。
// 保留：contentEditable 输入 + 发送/停止 + Enter 发送/Shift+Enter 换行 + 输入法兼容。
// 裁剪：增强提示(Enhance) / 换模型 / 附件上传 / skills 斜杠命令（与本系统无关）。
// 纯 UI 组件，不含业务：知识库选择、回答风格、store 对接都在 MessageInput 装配层。
interface Props {
  onSend: (text: string) => void
  onStop: () => void
  streaming: boolean
  placeholder?: string
}

export default function PromptInput({ onSend, onStop, streaming, placeholder }: Props) {
  const editorRef = useRef<HTMLDivElement>(null)
  const [hasText, setHasText] = useState(false)

  const syncText = () => {
    const text = editorRef.current?.textContent ?? ''
    setHasText(text.trim().length > 0)
  }

  const doSend = () => {
    const text = (editorRef.current?.textContent ?? '').trim()
    if (!text || streaming) return
    onSend(text)
    if (editorRef.current) editorRef.current.innerHTML = ''
    setHasText(false)
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    // 中文输入法组合期（拼音选词）按 Enter 是确认候选字，不是发送，必须跳过
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      doSend()
    }
  }

  // 粘贴只取纯文本，避免带入 Word/网页的富文本格式
  const onPaste = (e: React.ClipboardEvent) => {
    e.preventDefault()
    const text = e.clipboardData.getData('text/plain')
    document.execCommand('insertText', false, text)
  }

  const sendActive = hasText && !streaming

  return (
    <div className="pi-wrap">
      <div className="pi-frame">
        <div className="pi-editor-wrap">
          <div
            ref={editorRef}
            className="pi-field"
            contentEditable
            suppressContentEditableWarning
            role="textbox"
            aria-multiline="true"
            aria-label="输入问题"
            data-placeholder={placeholder ?? '输入你的问题，Enter 发送，Shift+Enter 换行'}
            data-empty={!hasText || undefined}
            onInput={syncText}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
          />
        </div>
        <div className="pi-row">
          <div className="pi-right">
            {streaming ? (
              <button type="button" className="pi-send pi-send-stop" onClick={onStop} aria-label="停止生成">
                <StopOutlined />
              </button>
            ) : (
              <button
                type="button"
                className={sendActive ? 'pi-send pi-send-active' : 'pi-send'}
                onClick={doSend}
                disabled={!sendActive}
                aria-label="发送"
              >
                <ArrowUpOutlined />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
