import { theme, type ThemeConfig } from 'antd'

// 科技风品牌色：电光青蓝 + 辅助紫（渐变用）
export const brandColor = '#00c6ff'
export const accentColor = '#7a6bff'

// AntD 5 深色科技主题：darkAlgorithm + 科技蓝青 + 玻璃拟态
export const themeConfig: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: brandColor,
    colorInfo: brandColor,
    colorLink: brandColor,
    // 深色基底（带轻透明，形成玻璃感）
    colorBgBase: '#0a0f1e',
    colorBgLayout: '#0a0f1e',
    colorBgContainer: 'rgba(20, 32, 58, 0.72)',
    colorBgElevated: 'rgba(24, 38, 68, 0.92)',
    colorBorder: 'rgba(122, 190, 255, 0.18)',
    colorBorderSecondary: 'rgba(122, 190, 255, 0.1)',
    // 文字：冷白偏蓝，避免纯白刺眼
    colorText: 'rgba(228, 241, 255, 0.92)',
    colorTextSecondary: 'rgba(163, 190, 220, 0.72)',
    // 语义色统一电光系
    colorSuccess: '#2ee6b8',
    colorError: '#ff5c7a',
    colorWarning: '#ffc25e',
    borderRadius: 8,
  },
  components: {
    Layout: {
      siderBg: 'rgba(10, 17, 34, 0.82)',
      headerBg: 'rgba(10, 17, 34, 0.6)',
      headerHeight: 56,
    },
  },
}
