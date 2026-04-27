import React, { useMemo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkFrontmatter from 'remark-frontmatter';
import remarkGfm from 'remark-gfm';

function injectToken(url: string): string {
    if (url.startsWith('/api/agents/')) {
        const token = localStorage.getItem('token');
        if (token && !url.includes('token=')) {
            return url + (url.includes('?') ? '&' : '?') + `token=${token}`;
        }
    }
    return url;
}

const components: Components = {
    img({ src, alt, ...props }) {
        const finalUrl = injectToken(src || '');
        return (
            <a href={finalUrl} target="_blank" rel="noopener noreferrer">
                <img
                    src={finalUrl}
                    alt={alt || ''}
                    style={{
                        maxWidth: '100%',
                        maxHeight: 400,
                        borderRadius: 4,
                        margin: '8px 0',
                        objectFit: 'contain',
                        cursor: 'pointer',
                    }}
                    {...props}
                />
            </a>
        );
    },
    a({ href, children, ...props }) {
        const finalUrl = injectToken(href || '');
        return (
            <a
                href={finalUrl}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: 'var(--accent-primary)' }}
                {...props}
            >
                {children}
            </a>
        );
    },
};

const remarkPlugins = [remarkFrontmatter, remarkGfm];

interface MarkdownRendererProps {
    content: string;
    style?: React.CSSProperties;
    className?: string;
    /** When true, YAML frontmatter is rendered as a visible code block instead of being stripped */
    showFrontmatter?: boolean;
}

function convertFrontmatter(content: string): string {
    const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
    if (!match) return content;
    const frontmatter = match[1];
    const rest = content.slice(match[0].length);
    return '```yaml\n' + frontmatter + '\n```\n\n' + rest;
}

export const MarkdownRenderer = React.memo(function MarkdownRenderer({ content, style, className, showFrontmatter }: MarkdownRendererProps) {
    const memoContent = useMemo(() => showFrontmatter ? convertFrontmatter(content) : content, [content, showFrontmatter]);
    return (
        <div
            className={`markdown-body ${className || ''}`}
            style={{ lineHeight: 1.6, fontSize: 'inherit', ...style, wordBreak: 'break-word' }}
        >
            <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
                {memoContent}
            </ReactMarkdown>
        </div>
    );
});

export default MarkdownRenderer;
