import type { ThemeConfig } from 'antd'

// 水利蓝品牌色
export const brandColor = '#1677ff'

// AntD 5 主题配置：中文语义 + 品牌色
export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: brandColor,
    colorInfo: brandColor,
    borderRadius: 8,
    colorBgLayout: '#f5f7fa',
  },
  components: {
    Layout: {
      siderBg: '#ffffff',
      headerBg: '#ffffff',
      headerHeight: 56,
    },
  },
}
