import { useState, useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../services/api';
import { useAuthStore } from '../stores';
import MarkdownRenderer from '../components/MarkdownRenderer';

/* ── Inline SVG Icons ── */
const Icons = {
    bot: (
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="5" width="12" height="10" rx="2" />
            <circle cx="7" cy="10" r="1" fill="currentColor" stroke="none" />
            <circle cx="11" cy="10" r="1" fill="currentColor" stroke="none" />
            <path d="M9 2v3M6 2h6" />
        </svg>
    ),
    user: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="8" cy="5.5" r="2.5" />
            <path d="M3 14v-1a4 4 0 018 0v1" />
        </svg>
    ),
    chat: (
        <svg width="28" height="28" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 3a1 1 0 011-1h10a1 1 0 011 1v7a1 1 0 01-1 1H5l-3 3V3z" />
            <path d="M5 5.5h6M5 8h4" />
        </svg>
    ),
    clip: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M13.5 7l-5.8 5.8a3 3 0 01-4.2-4.2L9.3 2.8a2 2 0 012.8 2.8L6.3 11.4a1 1 0 01-1.4-1.4L10.7 4.2" />
        </svg>
    ),
    loader: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M8 2v3M8 11v3M3.8 3.8l2.1 2.1M10.1 10.1l2.1 2.1M2 8h3M11 8h3M3.8 12.2l2.1-2.1M10.1 5.9l2.1-2.1" />
        </svg>
    ),
    tool: (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10.5 10.5L14 14M4.5 2a2.5 2.5 0 00-1.8 4.2l5.1 5.1A2.5 2.5 0 1012 7.2L6.8 2.2A2.5 2.5 0 004.5 2z" />
        </svg>
    ),
};

interface ToolCall {
    name: string;
    args: any;
    result?: string;
}

interface Message {
    role: 'user' | 'assistant';
    content: string;
    fileName?: string;
    toolCalls?: ToolCall[];
}

export default function Chat() {
    const { t } = useTranslation();
    const { id } = useParams<{ id: string }>();
    const token = useAuthStore((s) => s.token);
    const [messages, setMessages] = useState<Message[]>([]);
    const [input, setInput] = useState('');
    const [connected, setConnected] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [streaming, setStreaming] = useState(false);
    const [attachedFile, setAttachedFile] = useState<{ name: string; text: string; path?: string } | null>(null);
    const wsRef = useRef<WebSocket | null>(null);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const pendingToolCalls = useRef<ToolCall[]>([]);
    const streamContent = useRef('');

    const { data: agent } = useQuery({
        queryKey: ['agent', id],
        queryFn: () => agentApi.get(id!),
        enabled: !!id,
    });

    // Parse message content that may include [file:name] prefix from saved history
    const parseMessage = (msg: Message): Message => {
        if (msg.role !== 'user') return msg;
        const m = msg.content.match(/^\[file:([^\]]+)\]\n?/);
        if (m) return { ...msg, fileName: m[1], content: msg.content.slice(m[0].length) };
        return msg;
    };

    // Load chat history on mount
    useEffect(() => {
        if (!id || !token) return;
        fetch(`/api/chat/${id}/history`, {
            headers: { Authorization: `Bearer ${token}` },
        })
            .then(r => r.json())
            .then((history: Message[]) => {
                if (history.length > 0) setMessages(history.map(parseMessage));
            })
            .catch(() => { /* ignore */ });
    }, [id, token]);

    useEffect(() => {
        if (!id || !token) return;

        let cancelled = false;

        const connect = () => {
            if (cancelled) return;
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws/chat/${id}?token=${token}`;
            const ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                if (cancelled) {
                    ws.close();
                    return;
                }
                setConnected(true);
                wsRef.current = ws;
            };
            ws.onclose = () => {
                if (!cancelled) {
                    setConnected(false);
                    setTimeout(() => connect(), 2000);
                }
            };
            ws.onerror = () => {
                if (!cancelled) setConnected(false);
            };
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.type === 'chunk') {
                    // Streaming text chunk — accumulate and update live preview
                    streamContent.current += data.content;
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last && last.role === 'assistant' && last === prev[prev.length - 1]) {
                            // Update the streaming message in-place
                            const updated = [...prev];
                            updated[updated.length - 1] = { ...last, content: streamContent.current };
                            return updated;
                        }
                        return [...prev, { role: 'assistant', content: streamContent.current }];
                    });
                } else if (data.type === 'tool_call') {
                    if (data.status === 'done') {
                        pendingToolCalls.current.push({ name: data.name, args: data.args, result: data.result });
                    }
                } else if (data.type === 'done') {
                    // Final response — replace streaming message with final + tool calls
                    const toolCalls = pendingToolCalls.current.length > 0 ? [...pendingToolCalls.current] : undefined;
                    pendingToolCalls.current = [];
                    streamContent.current = '';
                    setStreaming(false);
                    setMessages(prev => {
                        const updated = [...prev];
                        // Replace the last streaming assistant message
                        if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
                            updated[updated.length - 1] = { role: 'assistant', content: data.content, toolCalls };
                        } else {
                            updated.push({ role: 'assistant', content: data.content, toolCalls });
                        }
                        return updated;
                    });
                } else {
                    // Legacy format: {role, content}
                    setMessages(prev => [...prev, { role: data.role, content: data.content }]);
                }
            };
        };

        connect();

        return () => {
            cancelled = true;
            if (wsRef.current) {
                wsRef.current.close();
                wsRef.current = null;
            }
        };
    }, [id, token]);

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        setUploading(true);
        try {
            const formData = new FormData();
            formData.append('file', file);
            if (id) formData.append('agent_id', id);

            const resp = await fetch('/api/chat/upload', {
                method: 'POST',
                headers: { Authorization: `Bearer ${token}` },
                body: formData,
            });

            if (!resp.ok) {
                const err = await resp.json();
                alert(err.detail || t('agent.upload.failed'));
                return;
            }

            const data = await resp.json();
            setAttachedFile({ name: data.filename, text: data.extracted_text, path: data.workspace_path });
        } catch (err) {
            alert(t('agent.upload.failed') + ': ' + (err as Error).message);
        } finally {
            setUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

    const sendMessage = () => {
        if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
        if (!input.trim() && !attachedFile) return;

        // Reset streaming state for new response
        pendingToolCalls.current = [];
        streamContent.current = '';
        setStreaming(true);

        let userMsg = input.trim();
        let contentForLLM = userMsg;

        if (attachedFile) {
            const wsPath = attachedFile.path || '';
            const codePath = wsPath.replace(/^workspace\//, '');
            const fileLoc = wsPath ? `\nFile location: ${wsPath} (for read_file/read_document tools)\nIn execute_code, use relative path: "${codePath}" (working directory is workspace/)` : '';
            const fileContext = `[文件: ${attachedFile.name}]${fileLoc}\n\n${attachedFile.text}`;
            contentForLLM = userMsg
                ? `${fileContext}\n\n用户问题: ${userMsg}`
                : `请阅读并分析以下文件内容:\n\n${fileContext}`;
            userMsg = userMsg || `[${t('agent.chat.attachment')}] ${attachedFile.name}`;
        }

        setMessages((prev) => [...prev, {
            role: 'user',
            content: userMsg,
            fileName: attachedFile?.name,
        }]);
        wsRef.current.send(JSON.stringify({ content: contentForLLM, display_content: userMsg, file_name: attachedFile?.name || '' }));
        setInput('');
        setAttachedFile(null);
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    };

    return (
        <div>
            <div className="page-header">
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <div style={{ width: '36px', height: '36px', borderRadius: 'var(--radius-md)', background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-tertiary)' }}>
                        {Icons.bot}
                    </div>
                    <div>
                        <h1 className="page-title" style={{ fontSize: '18px' }}>{agent?.name || '...'}</h1>
                        <div style={{ fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span className={`status-dot ${connected ? 'running' : 'stopped'}`} />
                            <span style={{ color: 'var(--text-tertiary)' }}>{connected ? t('agent.chat.connected') : t('agent.chat.disconnected')}</span>
                        </div>
                    </div>
                </div>
            </div>

            <div className="chat-container">
                <div className="chat-messages">
                    {messages.length === 0 && (
                        <div style={{ textAlign: 'center', padding: '60px', color: 'var(--text-tertiary)' }}>
                            <div style={{ marginBottom: '12px', display: 'flex', justifyContent: 'center' }}>{Icons.chat}</div>
                            <div>{t('agent.chat.startConversation', { name: agent?.name || t('nav.newAgent') })}</div>
                            <div style={{ fontSize: '12px', marginTop: '8px', opacity: 0.7 }}>{t('agent.chat.fileSupport')}</div>
                        </div>
                    )}
                    {messages.map((msg, i) => (
                        <div key={i} className={`chat-message ${msg.role}`}>
                            <div className="chat-avatar" style={{ color: 'var(--text-tertiary)' }}>
                                {msg.role === 'user' ? Icons.user : Icons.bot}
                            </div>
                            <div className="chat-bubble">
                                {msg.fileName && (
                                    <div style={{ fontSize: '11px', color: 'var(--accent-text)', marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                                        <span style={{ display: 'flex' }}>{Icons.clip}</span> {msg.fileName}
                                    </div>
                                )}
                                {msg.toolCalls && msg.toolCalls.length > 0 && (
                                    <details style={{
                                        marginBottom: '8px', fontSize: '12px',
                                        background: 'var(--accent-subtle)', borderRadius: '6px',
                                        padding: '0',
                                    }}>
                                        <summary style={{
                                            padding: '6px 10px', cursor: 'pointer',
                                            color: 'var(--accent-text)', fontWeight: 500,
                                            userSelect: 'none',
                                        }}>
                                            {Icons.tool} {msg.toolCalls.length} tool call{msg.toolCalls.length > 1 ? 's' : ''}
                                        </summary>
                                        <div style={{ padding: '4px 10px 8px' }}>
                                            {msg.toolCalls.map((tc, j) => (
                                                <div key={j} style={{
                                                    marginBottom: j < msg.toolCalls!.length - 1 ? '6px' : 0,
                                                    borderBottom: j < msg.toolCalls!.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                                                    paddingBottom: j < msg.toolCalls!.length - 1 ? '6px' : 0,
                                                }}>
                                                    <div style={{ fontWeight: 600, color: 'var(--accent-text)', marginBottom: '2px' }}>
                                                        {tc.name}
                                                    </div>
                                                    <div style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                                                        {JSON.stringify(tc.args)}
                                                    </div>
                                                    {tc.result && (
                                                        <div style={{
                                                            marginTop: '4px', fontSize: '11px', color: 'var(--text-secondary)',
                                                            fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                                                            maxHeight: '120px', overflow: 'auto',
                                                        }}>
                                                            {tc.result}
                                                        </div>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    </details>
                                )}
                                {msg.role === 'assistant' ? (
                                    <MarkdownRenderer content={msg.content} />
                                ) : (
                                    <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
                                )}
                            </div>
                        </div>
                    ))}
                    <div ref={messagesEndRef} />
                </div>

                {attachedFile && (
                    <div style={{
                        padding: '6px 12px',
                        background: 'var(--bg-elevated)',
                        borderTop: '1px solid var(--border-subtle)',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        fontSize: '12px',
                    }}>
                        <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}><span style={{ display: 'flex' }}>{Icons.clip}</span> {attachedFile.name}</span>
                        <button
                            onClick={() => setAttachedFile(null)}
                            style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', cursor: 'pointer', fontSize: '14px' }}
                        >✕</button>
                    </div>
                )}

                <div className="chat-input-area">
                    <input
                        type="file"
                        ref={fileInputRef}
                        onChange={handleFileSelect}
                        style={{ display: 'none' }}

                    />
                    <button
                        className="btn btn-secondary"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={!connected || uploading}
                        style={{ padding: '8px 12px', fontSize: '16px', minWidth: 'auto' }}
                        title={t('agent.workspace.uploadFile')}
                    >
                        {uploading ? Icons.loader : Icons.clip}
                    </button>
                    <input
                        className="chat-input"
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder={attachedFile ? t('agent.chat.askAboutFile', { name: attachedFile.name }) : t('chat.placeholder')}
                        disabled={!connected}
                    />
                    <button className="btn btn-primary" onClick={sendMessage} disabled={!connected || (!input.trim() && !attachedFile)}>
                        {t('chat.send')}
                    </button>
                </div>
            </div>
        </div>
    );
}
