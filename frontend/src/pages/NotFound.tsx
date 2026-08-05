import { Button, Result } from 'antd'
import { Link } from 'react-router-dom'

export default function NotFound() {
  return (
    <Result
      status="404"
      title="404"
      subTitle="抱歉，您访问的页面不存在"
      extra={
        <Link to="/chat">
          <Button type="primary">返回问答</Button>
        </Link>
      }
    />
  )
}
