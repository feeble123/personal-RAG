// P0-1：401 重放原请求的标记（自定义字段）
import 'axios'

declare module 'axios' {
  export interface InternalAxiosRequestConfig {
    _retried?: boolean
  }
}
