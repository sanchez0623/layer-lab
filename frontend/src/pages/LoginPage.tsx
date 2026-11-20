import { useState, type FormEvent } from 'react';
import { Layers3, Loader2, LogIn } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ApiError } from '../lib/api';
import { useAuth } from '../context/AuthContext';

/** 登录页：应用级登录入口，替代 nginx HTTP Basic 认证。
 *  密码错误（401）与网络异常（无法连后端）区分文案，登录成功跳转 #/backtest。 */
export default function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await login(username.trim(), password, remember);
      window.location.hash = '/backtest';
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.isNetworkError) {
          setError('无法连接服务器，请检查网络或稍后再试。');
        } else if (err.status === 401 || err.status === 400) {
          setError('用户名或密码错误，请重新输入。');
        } else {
          setError(err.message || '登录失败，请稍后再试。');
        }
      } else {
        setError('登录失败，请稍后再试。');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background px-4">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.35]"
        style={{
          backgroundImage:
            'radial-gradient(circle at 18% 22%, color-mix(in oklch, var(--primary) 28%, transparent), transparent 42%), radial-gradient(circle at 82% 78%, color-mix(in oklch, var(--primary) 18%, transparent), transparent 45%)',
        }}
      />
      <div className="relative w-full max-w-[400px]">
        <div className="mb-6 flex flex-col items-center gap-2.5">
          <div className="grid size-12 place-items-center rounded-[12px] bg-primary text-primary-foreground shadow-sm shadow-primary/20">
            <Layers3 size={28} strokeWidth={1.8} />
          </div>
          <div className="text-center">
            <strong className="block text-2xl font-semibold tracking-[-0.02em] text-foreground">LayerLab</strong>
            <span className="block text-[13px] text-muted-foreground">分层交易策略实验室</span>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>登录工作空间</CardTitle>
            <CardDescription>请使用您的账号登录，以访问回测 / 模拟交易与数据中心。</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="login-username">用户名</Label>
                <Input
                  id="login-username"
                  autoComplete="username"
                  placeholder="请输入用户名"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  disabled={submitting}
                  required
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="login-password">密码</Label>
                <Input
                  id="login-password"
                  type="password"
                  autoComplete="current-password"
                  placeholder="请输入密码"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={submitting}
                  required
                />
              </div>

              <Label htmlFor="login-remember" className="cursor-pointer text-[13px] font-normal text-muted-foreground">
                <Checkbox
                  id="login-remember"
                  checked={remember}
                  onCheckedChange={(checked) => setRemember(checked === true)}
                  disabled={submitting}
                />
                记住密码（7 天内免登录）
              </Label>

              {error && (
                <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
                  {error}
                </p>
              )}

              <Button type="submit" size="lg" className="w-full" disabled={submitting || !username.trim() || !password}>
                {submitting ? <Loader2 className="animate-spin" data-icon="inline-start" /> : <LogIn data-icon="inline-start" />}
                {submitting ? '登录中…' : '登录'}
              </Button>
            </form>
          </CardContent>
        </Card>

        <p className="mt-5 text-center text-[11px] text-muted-foreground">数据与结果不构成投资建议</p>
      </div>
    </div>
  );
}
