import { useState, useEffect } from "react"
import { Button } from "../components/ui/button"
import { Trash2, Plus, RefreshCw, Bot, ShieldCheck } from "lucide-react"
import { toast } from "sonner"

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<any[]>([])
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [registering, setRegistering] = useState(false)
  const [verifying, setVerifying] = useState<string | null>(null)

  const fetchAccounts = () => {
    fetch("http://localhost:8080/api/admin/accounts", { headers: { Authorization: "Bearer admin" } })
      .then(res => res.json())
      .then(data => setAccounts(data.accounts || []))
      .catch(() => toast.error("刷新失败：无法连接后端服务"))
  }

  useEffect(() => {
    fetchAccounts()
  }, [])

  const handleAdd = () => {
    if (!email || !password) {
      toast.error("邮箱和密码不能为空")
      return
    }
    const id = toast.loading("正在添加账号...")
    fetch("http://localhost:8080/api/admin/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer admin" },
      body: JSON.stringify({ email, password, token: "", valid: true })
    }).then(res => res.json())
      .then(data => {
        if(data.status === "success" || data.ok) {
          toast.success("添加成功", { id })
          setEmail("")
          setPassword("")
          fetchAccounts()
        } else {
          toast.error("添加失败", { id })
        }
      }).catch(() => toast.error("网络错误", { id }))
  }

  const handleDelete = (emailToDelete: string) => {
    const id = toast.loading("正在删除...")
    fetch(`http://localhost:8080/api/admin/accounts/${encodeURIComponent(emailToDelete)}`, {
      method: "DELETE",
      headers: { Authorization: "Bearer admin" }
    }).then(() => {
      toast.success("账号已删除", { id })
      fetchAccounts()
    }).catch(() => toast.error("删除失败", { id }))
  }
  
  const handleAutoRegister = () => {
    setRegistering(true)
    const id = toast.loading("浏览器无头注册引擎已拉起，正在全自动获取新千问账号，预计耗时 1~2 分钟，请勿关闭页面...")
    fetch("http://localhost:8080/api/admin/accounts/register", {
      method: "POST",
      headers: { Authorization: "Bearer admin" }
    }).then(res => res.json())
      .then(data => {
        if(data.ok) {
          toast.success(`全自动注册成功！已提取 Token 并入池：${data.email}`, { id, duration: 8000 })
          fetchAccounts()
        } else {
          toast.error(data.error || "自动化注册被阿里风控阻截", { id })
        }
      })
      .catch(() => toast.error("连接超时或注册异常终止", { id }))
      .finally(() => setRegistering(false))
  }
  
  const handleVerify = (emailToVerify: string) => {
    setVerifying(emailToVerify)
    const id = toast.loading(`正在强制验活账号: ${emailToVerify}...`)
    fetch(`http://localhost:8080/api/admin/accounts/${encodeURIComponent(emailToVerify)}/verify`, {
      method: "POST",
      headers: { Authorization: "Bearer admin" }
    }).then(res => res.json())
      .then(data => {
        if(data.valid) toast.success(`账号存活，Token 健康！`, { id })
        else toast.error(`账号已死，或触发限流`, { id })
        fetchAccounts()
      })
      .catch(() => toast.error("验证请求失败", { id }))
      .finally(() => setVerifying(null))
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">账号管理</h2>
          <p className="text-muted-foreground">管理通义千问上游账号池。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => { fetchAccounts(); toast.success("大盘数据已刷新"); }}>
            <RefreshCw className="mr-2 h-4 w-4" /> 刷新状态
          </Button>
          <Button variant="default" onClick={handleAutoRegister} disabled={registering} className="bg-blue-600 hover:bg-blue-700">
            {registering ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : <Bot className="mr-2 h-4 w-4" />}
            {registering ? "注册引擎运行中..." : "一键自动化获取新号"}
          </Button>
        </div>
      </div>

      <div className="flex gap-4 items-end bg-card p-4 rounded-xl border">
        <div className="flex-1">
          <label className="text-sm font-medium mb-1 block">手动录入邮箱</label>
          <input 
            type="text" 
            value={email} 
            onChange={e => setEmail(e.target.value)} 
            className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" 
            placeholder="例如: test@example.com" 
          />
        </div>
        <div className="flex-1">
          <label className="text-sm font-medium mb-1 block">密码</label>
          <input 
            type="password" 
            value={password} 
            onChange={e => setPassword(e.target.value)} 
            className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" 
            placeholder="密码" 
          />
        </div>
        <Button onClick={handleAdd} variant="secondary">
          <Plus className="mr-2 h-4 w-4" /> 手动注入
        </Button>
      </div>

      <div className="rounded-xl border bg-card overflow-hidden">
        <table className="w-full text-sm text-left">
          <thead className="bg-muted/50 border-b text-muted-foreground">
            <tr>
              <th className="h-12 px-4 align-middle font-medium">账号</th>
              <th className="h-12 px-4 align-middle font-medium">状态</th>
              <th className="h-12 px-4 align-middle font-medium">正在处理请求数</th>
              <th className="h-12 px-4 align-middle font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {accounts.length === 0 && (
              <tr>
                <td colSpan={4} className="p-4 text-center text-muted-foreground">暂无账号数据</td>
              </tr>
            )}
            {accounts.map(acc => (
              <tr key={acc.email} className="border-b transition-colors hover:bg-muted/50">
                <td className="p-4 align-middle font-medium">{acc.email}</td>
                <td className="p-4 align-middle">
                  {acc.valid ? (
                    <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-green-100 text-green-800">
                      Token 有效
                    </span>
                  ) : (
                    <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-red-100 text-red-800">
                      失效/待刷新
                    </span>
                  )}
                </td>
                <td className="p-4 align-middle">{acc.inflight} 并发</td>
                <td className="p-4 align-middle text-right space-x-2">
                  <Button variant="outline" size="sm" onClick={() => handleVerify(acc.email)} disabled={verifying === acc.email}>
                    {verifying === acc.email ? <RefreshCw className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => handleDelete(acc.email)} className="text-destructive hover:bg-destructive/10 hover:text-destructive">
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
