// ESLint 9 flat config：TypeScript + React Hooks + react-refresh 官方推荐规则。
// 与 Vite 官方 React-TS 模板对齐，仅关闭对「生产构建」无影响的规则（见下注释）。
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  // 忽略构建产物与生成文件
  { ignores: ['dist', 'node_modules', '*.config.js', 'vite.config.d.ts'] },

  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // 未使用变量/参数：TS 编译层已由 noUnusedLocals:false 放行（历史代码存在未用导入），
      // 这里同样放宽为 warn，避免 lint 强制清零阻塞开发；但未用「私有」变量仍提示。
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      // 显式 any：历史代码（API 返回类型宽松）存在，设为 warn 而非 error，逐步收紧。
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
)
