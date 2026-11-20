import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, clearAuthToken, getAuthToken, setAuthToken } from '../lib/api';

/** 当前登录用户（应用级登录）。 */
export interface AuthUser {
  username: string;
}

interface AuthContextValue {
  user: AuthUser | null;
  /** 挂载时校验令牌期间为 true，用于渲染 Loading，避免闪登录页。 */
  initializing: boolean;
  /** 登录：remember=true 令牌跨刷新保持（7 天），否则关浏览器即失效。 */
  login: (username: string, password: string, remember: boolean) => Promise<AuthUser>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const UNAUTHORIZED_EVENT = 'layerlab:unauthorized';

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [initializing, setInitializing] = useState(true);

  const handleUnauthorized = useCallback(() => {
    clearAuthToken();
    setUser(null);
  }, []);

  // 挂载时若有令牌则校验；401 由 api 层清令牌并广播 unauthorized 事件。
  useEffect(() => {
    const token = getAuthToken();
    if (!token) {
      setInitializing(false);
      return;
    }
    let mounted = true;
    api.me()
      .then((me) => { if (mounted) setUser({ username: me.username }); })
      .catch(() => { if (mounted) handleUnauthorized(); })
      .finally(() => { if (mounted) setInitializing(false); });
    return () => { mounted = false; };
  }, [handleUnauthorized]);

  // 监听全局 401 事件：任意受保护请求被后端拒绝时自动登出并跳登录页。
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
  }, [handleUnauthorized]);

  const login = useCallback(async (username: string, password: string, remember: boolean): Promise<AuthUser> => {
    const res = await api.login({ username, password, remember_me: remember });
    setAuthToken(res.token, remember);
    const next: AuthUser = { username: res.username };
    setUser(next);
    return next;
  }, []);

  const logout = useCallback(() => {
    // 尽力通知后端撤销令牌；无论成功与否都清本地凭证回到登录页。
    void api.logout().catch(() => { /* 后端已失效也无所谓 */ });
    handleUnauthorized();
  }, [handleUnauthorized]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, initializing, login, logout }),
    [user, initializing, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 <AuthProvider> 内使用');
  return ctx;
}
