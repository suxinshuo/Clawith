import { useState, useEffect, useCallback } from 'react';
import { useAuthStore } from '../stores/authStore';

const API_BASE = import.meta.env.VITE_API_BASE || '';

interface Credential {
  id: string;
  provider: string;
  credential_type: string;
  status: string;
  display_name: string | null;
  external_username: string | null;
  scopes: string | null;
  last_used_at: string | null;
  created_at: string;
}

interface Provider {
  provider: string;
  scopes: string;
  has_oauth: boolean;
}

export default function ExternalConnections() {
  const token = useAuthStore((s) => s.token);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [addProvider, setAddProvider] = useState('');
  const [addApiKey, setAddApiKey] = useState('');
  const [loading, setLoading] = useState(true);
  const [oauthSuccess] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get('oauth_success') === '1' ? params.get('provider') || '' : '';
  });

  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };

  const fetchData = useCallback(async () => {
    try {
      const [credRes, provRes] = await Promise.all([
        fetch(`${API_BASE}/api/credentials/me`, { headers }),
        fetch(`${API_BASE}/api/credentials/providers`, { headers }),
      ]);
      if (credRes.ok) setCredentials(await credRes.json());
      if (provRes.ok) setProviders(await provRes.json());
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleDelete = async (id: string) => {
    if (!confirm('确定断开此连接？')) return;
    await fetch(`${API_BASE}/api/credentials/${id}`, { method: 'DELETE', headers });
    fetchData();
  };

  const handleAddManual = async () => {
    if (!addProvider.trim() || !addApiKey.trim()) return;
    await fetch(`${API_BASE}/api/credentials/manual`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ provider: addProvider, access_token: addApiKey }),
    });
    setShowAdd(false);
    setAddProvider('');
    setAddApiKey('');
    fetchData();
  };

  const handleOAuthConnect = async (provider: string) => {
    try {
      const res = await fetch(
        `${API_BASE}/api/credentials/oauth/authorize?provider=${provider}`,
        { headers },
      );
      if (res.ok) {
        const { authorize_url } = await res.json();
        window.location.href = authorize_url;
      }
    } catch (e) {
      console.error('Failed to start OAuth flow:', e);
    }
  };

  if (loading) return <div style={{ padding: 24 }}>加载中...</div>;

  const statusLabel: Record<string, string> = {
    active: '已连接',
    expired: '已过期',
    needs_reauth: '需重新授权',
    revoked: '已撤销',
  };

  return (
    <div style={{ maxWidth: 700, margin: '0 auto', padding: 24 }}>
      <h2>外部系统连接</h2>
      {oauthSuccess && (
        <div style={{ padding: '12px 16px', marginBottom: 16, background: '#064e3b', borderRadius: 8, color: '#6ee7b7' }}>
          {oauthSuccess} 已连接成功。
        </div>
      )}
      <p style={{ color: '#888', marginBottom: 24 }}>管理您与外部系统的连接凭据。Agent 将以您的身份访问外部系统。</p>

      {credentials.length === 0 && <p style={{ color: '#666' }}>暂无已连接的外部系统。</p>}

      {credentials.map((c) => (
        <div key={c.id} style={{ border: '1px solid #333', borderRadius: 8, padding: 16, marginBottom: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <strong>{c.display_name || c.provider}</strong>
              <span style={{ marginLeft: 8, color: c.status === 'active' ? '#4ade80' : '#f87171', fontSize: 13 }}>
                {statusLabel[c.status] || c.status}
              </span>
            </div>
            <button onClick={() => handleDelete(c.id)} style={{ color: '#f87171', background: 'none', border: 'none', cursor: 'pointer' }}>
              断开连接
            </button>
          </div>
          {c.external_username && <div style={{ fontSize: 13, color: '#888', marginTop: 4 }}>{c.external_username}</div>}
          {c.scopes && <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>授权范围: {c.scopes}</div>}
          {c.last_used_at && <div style={{ fontSize: 12, color: '#666', marginTop: 2 }}>最后使用: {new Date(c.last_used_at).toLocaleString()}</div>}
        </div>
      ))}

      <div style={{ marginTop: 24 }}>
        <h3>添加连接</h3>

        {providers.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <p style={{ fontSize: 13, color: '#888', marginBottom: 8 }}>OAuth 授权连接：</p>
            {providers.map((p) => (
              <button
                key={p.provider}
                onClick={() => handleOAuthConnect(p.provider)}
                style={{ marginRight: 8, padding: '6px 16px', borderRadius: 6, border: '1px solid #555', background: '#2563eb', color: '#fff', cursor: 'pointer' }}
              >
                连接 {p.provider}
              </button>
            ))}
          </div>
        )}

        <button onClick={() => setShowAdd(!showAdd)} style={{ padding: '6px 16px', borderRadius: 6, border: '1px solid #555', background: 'transparent', color: '#ccc', cursor: 'pointer' }}>
          + 手动添加 API Key
        </button>

        {showAdd && (
          <div style={{ marginTop: 12, padding: 16, border: '1px solid #333', borderRadius: 8 }}>
            <div style={{ marginBottom: 12 }}>
              <label style={{ display: 'block', marginBottom: 4, fontSize: 13 }}>Provider 名称</label>
              <input
                value={addProvider} onChange={(e) => setAddProvider(e.target.value)} placeholder="如 jira, github, internal_erp"
                style={{ width: '100%', padding: '6px 10px', border: '1px solid #444', borderRadius: 6, background: '#1a1a1a', color: '#fff' }}
              />
            </div>
            <div style={{ marginBottom: 12 }}>
              <label style={{ display: 'block', marginBottom: 4, fontSize: 13 }}>API Key / Token</label>
              <input
                type="password" value={addApiKey} onChange={(e) => setAddApiKey(e.target.value)} placeholder="请输入 API Key"
                style={{ width: '100%', padding: '6px 10px', border: '1px solid #444', borderRadius: 6, background: '#1a1a1a', color: '#fff' }}
              />
            </div>
            <button onClick={handleAddManual} style={{ padding: '6px 16px', borderRadius: 6, background: '#2563eb', color: '#fff', border: 'none', cursor: 'pointer' }}>
              保存
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
