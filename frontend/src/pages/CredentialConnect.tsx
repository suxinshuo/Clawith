import { useState, useEffect } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE || '';

/**
 * Standalone credential submission page — no login required.
 * Opened via: /credentials/connect?token=<one-time-jwt>
 */
export default function CredentialConnect() {
  const [token] = useState(() => new URLSearchParams(window.location.search).get('token') || '');
  const [apiKey, setApiKey] = useState('');
  const [externalUserId, setExternalUserId] = useState('');
  const [status, setStatus] = useState<'idle' | 'submitting' | 'success' | 'error'>('idle');
  const [errorMessage, setErrorMessage] = useState('');
  const [provider, setProvider] = useState('');
  const [oauthSuccess] = useState(() => new URLSearchParams(window.location.search).get('oauth_success') === '1');
  const [oauthProvider] = useState(() => new URLSearchParams(window.location.search).get('provider') || '');

  useEffect(() => {
    if (token) {
      try {
        const payload = JSON.parse(atob(token.split('.')[1]));
        setProvider(payload.provider || '');

        // OAuth mode: auto-redirect to OAuth start endpoint
        if (payload.credential_mode === 'oauth') {
          window.location.href = `${API_BASE}/api/credentials/oauth/start?token=${token}`;
        }
      } catch {
        // ignore decode errors
      }
    }
  }, [token]);

  if (oauthSuccess) {
    return (
      <div style={{ maxWidth: 400, margin: '80px auto', padding: 24, textAlign: 'center' }}>
        <h2>授权成功</h2>
        <p>{oauthProvider ? `${oauthProvider} 已连接成功。` : '授权已完成。'}请回到对话继续。</p>
      </div>
    );
  }

  if (!token) {
    return (
      <div style={{ maxWidth: 400, margin: '80px auto', padding: 24, textAlign: 'center' }}>
        <h2>链接无效</h2>
        <p>缺少凭据配置 token，请从对话中重新获取链接。</p>
      </div>
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKey.trim()) return;

    setStatus('submitting');
    setErrorMessage('');

    try {
      const res = await fetch(`${API_BASE}/api/credentials/submit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token,
          access_token: apiKey.trim(),
          external_user_id: externalUserId.trim() || undefined,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      setStatus('success');
    } catch (err: any) {
      setStatus('error');
      setErrorMessage(err.message || 'Submission failed');
    }
  };

  if (status === 'success') {
    return (
      <div style={{ maxWidth: 400, margin: '80px auto', padding: 24, textAlign: 'center' }}>
        <h2>配置成功</h2>
        <p>{provider ? `${provider} 凭据已保存。` : '凭据已保存。'}请回到对话继续。</p>
      </div>
    );
  }

  return (
    <div style={{ maxWidth: 400, margin: '80px auto', padding: 24 }}>
      <h2>配置{provider ? ` ${provider} ` : ''}凭据</h2>
      <p style={{ color: '#888', fontSize: 14 }}>
        此链接仅可使用一次，10 分钟内有效。
      </p>

      <form onSubmit={handleSubmit}>
        <div style={{ marginBottom: 16 }}>
          <label style={{ display: 'block', marginBottom: 4, fontWeight: 500 }}>
            API Key / Token *
          </label>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="请输入 API Key 或 Token"
            required
            style={{
              width: '100%',
              padding: '8px 12px',
              border: '1px solid #333',
              borderRadius: 6,
              background: '#1a1a1a',
              color: '#fff',
              fontSize: 14,
            }}
          />
        </div>

        <div style={{ marginBottom: 16 }}>
          <label style={{ display: 'block', marginBottom: 4, fontWeight: 500 }}>
            外部系统用户 ID（可选）
          </label>
          <input
            type="text"
            value={externalUserId}
            onChange={(e) => setExternalUserId(e.target.value)}
            placeholder="如 john@company.com"
            style={{
              width: '100%',
              padding: '8px 12px',
              border: '1px solid #333',
              borderRadius: 6,
              background: '#1a1a1a',
              color: '#fff',
              fontSize: 14,
            }}
          />
        </div>

        {status === 'error' && (
          <p style={{ color: '#f44', fontSize: 14, marginBottom: 12 }}>{errorMessage}</p>
        )}

        <button
          type="submit"
          disabled={status === 'submitting' || !apiKey.trim()}
          style={{
            width: '100%',
            padding: '10px 0',
            background: status === 'submitting' ? '#555' : '#2563eb',
            color: '#fff',
            border: 'none',
            borderRadius: 6,
            fontSize: 15,
            cursor: status === 'submitting' ? 'not-allowed' : 'pointer',
          }}
        >
          {status === 'submitting' ? '提交中...' : '提交'}
        </button>
      </form>
    </div>
  );
}
