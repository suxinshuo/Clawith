import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from './stores';
import { useEffect, useState } from 'react';
import { authApi } from './services/api';
import Login from './pages/Login';
import Layout from './pages/Layout';
import Dashboard from './pages/Dashboard';
import Plaza from './pages/Plaza';
import AgentDetail from './pages/AgentDetail';
import AgentCreate from './pages/AgentCreate';
import Chat from './pages/Chat';
import Messages from './pages/Messages';
import EnterpriseSettings from './pages/EnterpriseSettings';
import InvitationCodes from './pages/InvitationCodes';

function ProtectedRoute({ children }: { children: React.ReactNode }) {
    const token = useAuthStore((s) => s.token);
    if (!token) return <Navigate to="/login" replace />;
    return <>{children}</>;
}

export default function App() {
    const { token, setAuth, user } = useAuthStore();
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        // Initialize theme on app mount (ensures login page gets correct theme)
        const savedTheme = localStorage.getItem('theme') || 'dark';
        document.documentElement.setAttribute('data-theme', savedTheme);

        if (token && !user) {
            authApi.me()
                .then((u) => setAuth(u, token))
                .catch(() => useAuthStore.getState().logout())
                .finally(() => setLoading(false));
        } else {
            setLoading(false);
        }
    }, []);

    if (loading) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', color: 'var(--text-tertiary)' }}>
                加载中...
            </div>
        );
    }

    return (
        <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/" element={<ProtectedRoute><Layout /></ProtectedRoute>}>
                <Route index element={<Navigate to="/plaza" replace />} />
                <Route path="dashboard" element={<Dashboard />} />
                <Route path="plaza" element={<Plaza />} />
                <Route path="agents/new" element={<AgentCreate />} />
                <Route path="agents/:id" element={<AgentDetail />} />
                <Route path="agents/:id/chat" element={<Chat />} />
                <Route path="messages" element={<Messages />} />
                <Route path="enterprise" element={<EnterpriseSettings />} />
                <Route path="invitations" element={<InvitationCodes />} />
            </Route>
        </Routes>
    );
}
