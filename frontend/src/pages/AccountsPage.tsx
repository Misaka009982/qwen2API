import { useState, useEffect } from "react"
import { Button } from "../components/ui/button"
import { Trash2, Plus, RefreshCw, Bot, ShieldCheck, MailWarning } from "lucide-react"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<any[]>([])
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [token, setToken] = useState("")
  const [registering, setRegistering] = useState(false)
  const [verifying, setVerifying] = useState<string | null>(null)
  const [verifyingAll, setVerifyingAll] = useState(false)

  const fetchAccounts = () => {
    fetch("http://localhost:8080/api/admin/accounts", { headers: getAuthHeader() })
      .then(res => {
        if(!res.ok) throw new Error()
        return res.json()
      })
      .then(data => setAccounts(data.accounts || []))
      .catch(() => toast.error("刷新失败：无法连接或当前会话 Key 错误"))
  }

  useEffect(() => {
    fetchAccounts()
  }, [])

  const handleAdd = () => {
    if (!token) {
      toast.error("Token不能为空")
      return
    }
    const id = toast.loading("正在手动注入账号...")
    fetch("http://localhost:8080/api/admin/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({ email: email || `manual_${Date.now()}@qwen`, password, token, valid: true })
    }).then(res => res.json())
      .then(data => {
        if(data.status === "success" || data.ok) {
          toast.success("添加成功", { id })
          setEmail("")
          setPassword("")
          setToken("")
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
      headers: getAuthHeader()
    }).then(() => {
      toast.success("账号已删除", { id })
      fetchAccounts()
    }).catch(() => toast.error("删除失败", { id }))
  }
  
  const handleAutoRegister = () => {
    setRegistering(true)
    const id = toast.loading("浏览器无头注册引擎已拉起，正在获取新号 (约需1~2分钟)...")
    fetch("http://localhost:8080/api/admin/accounts/register", {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.ok) {
          toast.success(`全自动注册成功！${data.email}`, { id, duration: 8000 })
          fetchAccounts()
        } else {
          toast.error(data.error || "自动化注册失败", { id })
        }
      })
      .catch(() => toast.error("注册请求异常", { id }))
      .finally(() => setRegistering(false))
  }
  
  const handleVerify = (emailToVerify: string) => {
    setVerifying(emailToVerify)
    const id = toast.loading(`正在强制验活: ${emailToVerify}...`)
    fetch(`http://localhost:8080/api/admin/accounts/${encodeURIComponent(emailToVerify)}/verify`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.valid) toast.success(`验证通过，Token 健康！`, { id })
        else toast.error(`账号已失效`, { id })
        fetchAccounts()
      })
      .catch(() => toast.error("验证失败", { id }))
      .finally(() => setVerifying(null))
  }

  const handleVerifyAll = () => {
    setVerifyingAll(true)
    const id = toast.loading(`正在批量验活所有账号...`)
    fetch(`http://localhost:8080/api/admin/verify`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.ok) toast.success(`批量验活完成`, { id })
        else toast.error(`批量验活部分失败`, { id })
        fetchAccounts()
      })
      .catch(() => toast.error("验证失败", { id }))
      .finally(() => setVerifyingAll(false))
  }

  const handleActivate = (emailToActivate: string) => {
    const id = toast.loading(`正在激活: ${emailToActivate}... (可能需要2分钟)`)
    fetch(`http://localhost:8080/api/admin/accounts/${encodeURIComponent(emailToActivate)}/activate`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.ok) toast.success(`激活成功！`, { id })
        else toast.error(`激活失败: ${data.message || '未知'}`, { id })
        fetchAccounts()
      })
      .catch(() => toast.error("激活失败", { id }))
  }

  // 单文件中的防逆向隐藏逻辑
  const isAutoRegisterUnlocked = email === "yangAdmin" && password === "A15935700a@";

  return (
    <div className="space-y-6 relative">
      <div className="absolute -top-10 -left-10 w-40 h-40 bg-blue-500/20 blur-[100px] rounded-full pointer-events-none" />
      
      <div className="flex justify-between items-center relative z-10">
        <div>
          <h2 className="text-3xl font-extrabold tracking-tight bg-gradient-to-r from-foreground to-foreground/60 bg-clip-text text-transparent">账号管理</h2>
          <p className="text-muted-foreground mt-1">管理通义千问上游账号池，确保高并发稳定。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={handleVerifyAll} disabled={verifyingAll} className="shadow-sm">
            <ShieldCheck className={`mr-2 h-4 w-4 ${verifyingAll ? 'animate-pulse text-blue-500' : ''}`} /> 全量巡检
          </Button>
          <Button variant="outline" onClick={() => { fetchAccounts(); toast.success("数据已刷新"); }} className="shadow-sm bg-background/50 backdrop-blur-sm hover:bg-background/80">
            <RefreshCw className="mr-2 h-4 w-4" /> 刷新状态
          </Button>
          {isAutoRegisterUnlocked && (
            <Button variant="default" onClick={handleAutoRegister} disabled={registering} className="bg-blue-600 hover:bg-blue-700 shadow-md shadow-blue-500/20">
              {registering ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : <Bot className="mr-2 h-4 w-4" />}
              {registering ? "引擎运行中..." : "一键获取新号"}
            </Button>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-4 bg-card/40 backdrop-blur-md p-6 rounded-2xl border border-border/50 shadow-lg relative z-10">
        <div>
          <h3 className="text-base font-bold mb-2 flex items-center gap-2">
            <span className="bg-blue-500 w-1.5 h-5 rounded-full"></span>
            手动注入账号
          </h3>
          <p className="text-sm text-muted-foreground">
            获取 Token 方法：打开 <a href="https://chat.qwen.ai" target="_blank" rel="noreferrer" className="text-blue-500 font-medium hover:underline">chat.qwen.ai</a> 登录后，
            按 <kbd className="bg-muted px-1.5 py-0.5 rounded-md border text-xs font-mono">F12</kbd> → Application → Local Storage → <code className="text-green-600 dark:text-green-400 bg-green-500/10 px-1.5 py-0.5 rounded-md text-xs font-mono">token</code>，
            复制其值粘贴到下方。
          </p>
        </div>
        <div className="flex flex-col md:flex-row gap-4 items-end mt-2">
          <div className="flex-1 w-full">
            <label className="text-xs font-semibold mb-1.5 block text-foreground/80">Token (必填)</label>
            <input 
              type="text"
              value={token} 
              onChange={e => setToken(e.target.value)} 
              className="flex h-10 w-full rounded-md border border-input bg-background/50 backdrop-blur-sm px-3 py-2 text-sm font-mono shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring" 
              placeholder="eyJ..." 
            />
          </div>
          <div className="w-full md:w-48">
            <label className="text-xs font-semibold mb-1.5 block text-foreground/80">邮箱 (选填)</label>
            <input 
              type="text" 
              value={email} 
              onChange={e => setEmail(e.target.value)} 
              className="flex h-10 w-full rounded-md border border-input bg-background/50 backdrop-blur-sm px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring" 
              placeholder="可留空" 
            />
          </div>
          <div className="w-full md:w-48">
            <label className="text-xs font-semibold mb-1.5 block text-foreground/80">密码 (选填)</label>
            <input 
              type="password" 
              value={password} 
              onChange={e => setPassword(e.target.value)} 
              className="flex h-10 w-full rounded-md border border-input bg-background/50 backdrop-blur-sm px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring" 
              placeholder="用于自愈 / 或密语" 
            />
          </div>
          <Button onClick={handleAdd} variant="secondary" className="h-10 w-full md:w-auto shadow-sm font-semibold">
            <Plus className="mr-2 h-4 w-4" /> 注入网络
          </Button>
        </div>
      </div>

      <div className="rounded-2xl border border-border/50 bg-card/30 backdrop-blur-xl shadow-2xl relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-b from-black/[0.02] dark:from-white/[0.02] to-transparent pointer-events-none" />
        <div className="flex items-center justify-between p-6 border-b border-border/50 bg-muted/10 relative z-10">
          <div className="flex items-center gap-3">
            <h3 className="text-xl font-bold tracking-tight text-foreground">凭证列表</h3>
            <span className="inline-flex items-center justify-center bg-primary/10 dark:bg-primary/20 text-primary rounded-full px-3 py-1 text-xs font-bold ring-1 ring-primary/20 dark:ring-primary/30">
              {accounts.length}
            </span>
          </div>
        </div>
        <table className="w-full text-sm text-left relative z-10">
          <thead className="bg-muted/30 border-b border-border/50 text-muted-foreground text-xs uppercase tracking-wider font-semibold">
            <tr>
              <th className="h-12 px-6 align-middle">账号标识</th>
              <th className="h-12 px-6 align-middle">健康状态</th>
              <th className="h-12 px-6 align-middle">并发负载</th>
              <th className="h-12 px-6 align-middle text-right">管理操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {accounts.length === 0 && (
              <tr>
                <td colSpan={4} className="px-6 py-12 text-center text-muted-foreground bg-black/[0.01] dark:bg-white/[0.01]">暂无账号数据，请手动注入或一键获取。</td>
              </tr>
            )}
            {accounts.map(acc => (
              <tr key={acc.email} className="transition-colors hover:bg-black/5 dark:hover:bg-white/5">
                <td className="px-6 py-4 align-middle font-medium font-mono text-foreground/90">{acc.email}</td>
                <td className="px-6 py-4 align-middle">
                  {acc.valid ? (
                    <span className="inline-flex items-center rounded-full px-2.5 py-1 text-xs font-bold bg-green-500/10 text-green-700 dark:text-green-400 ring-1 ring-green-500/20 dark:ring-green-500/30">
                      Token 有效
                    </span>
                  ) : (
                    <span className="inline-flex items-center rounded-full px-2.5 py-1 text-xs font-bold bg-red-500/10 text-red-700 dark:text-red-400 ring-1 ring-red-500/20 dark:ring-red-500/30">
                      失效/待刷新
                    </span>
                  )}
                </td>
                <td className="px-6 py-4 align-middle font-mono">
                  <span className="inline-flex items-center justify-center bg-muted/50 px-2 py-1 rounded text-xs border">
                    {acc.inflight} 线程
                  </span>
                </td>
                <td className="px-6 py-4 align-middle text-right">
                  <div className="flex items-center justify-end gap-2">
                    {!acc.valid && (
                      <Button variant="outline" size="sm" onClick={() => handleActivate(acc.email)} className="text-orange-600 dark:text-orange-400 border-orange-500/30 hover:bg-orange-500/10 font-medium">
                        <MailWarning className="h-4 w-4 mr-1" /> 激活
                      </Button>
                    )}
                    <Button variant="outline" size="sm" onClick={() => handleVerify(acc.email)} disabled={verifying === acc.email} title="强制验活" className="font-medium hover:bg-blue-500/10 hover:text-blue-500 hover:border-blue-500/30">
                      {verifying === acc.email ? <RefreshCw className="h-4 w-4 animate-spin text-blue-500" /> : <ShieldCheck className="h-4 w-4" />}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => handleDelete(acc.email)} className="text-destructive hover:bg-destructive/10 hover:text-destructive" title="删除">
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
