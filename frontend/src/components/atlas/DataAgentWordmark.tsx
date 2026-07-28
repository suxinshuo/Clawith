interface Props {
    /** Rendered height of the brand mark in px; the wordmark text scales with it */
    height?: number;
    className?: string;
}

/**
 * DataAgent wordmark — robot mark + "DataAgent" text.
 * The mark's shell uses fill="currentColor" and its highlights use var(--bg),
 * so theme switching (Paper Atlas ↔ Night Atlas) is automatic.
 */
export default function DataAgentWordmark({ height = 32, className }: Props) {
    const classes = ['atlas-wordmark', className].filter(Boolean).join(' ');
    return (
        <span className={classes} style={{ gap: Math.round(height * 0.34) }} aria-label="DataAgent">
            <svg
                xmlns="http://www.w3.org/2000/svg"
                viewBox="40 12 432 478"
                height={height}
                fill="none"
                aria-hidden="true"
                className="atlas-wordmark-mark"
            >
                {/* Antenna */}
                <line x1="256" y1="100" x2="256" y2="48" stroke="currentColor" strokeWidth="12" strokeLinecap="round" />
                <circle cx="256" cy="38" r="16" fill="currentColor" />
                {/* Ears */}
                <rect x="56" y="180" width="40" height="80" rx="20" fill="currentColor" />
                <rect x="416" y="180" width="40" height="80" rx="20" fill="currentColor" />
                {/* Head */}
                <rect x="96" y="100" width="320" height="280" rx="80" fill="currentColor" />
                {/* Eyes */}
                <circle cx="192" cy="230" r="36" fill="var(--bg)" />
                <circle cx="320" cy="230" r="36" fill="var(--bg)" />
                <circle cx="200" cy="224" r="14" fill="currentColor" />
                <circle cx="328" cy="224" r="14" fill="currentColor" />
                {/* Mouth */}
                <rect x="192" y="310" width="128" height="16" rx="8" fill="var(--bg)" opacity="0.7" />
                {/* Neck */}
                <rect x="216" y="380" width="80" height="40" rx="8" fill="currentColor" />
                {/* Shoulders */}
                <rect x="136" y="420" width="240" height="60" rx="30" fill="currentColor" />
            </svg>
            <span className="atlas-wordmark-name" style={{ fontSize: Math.round(height * 0.78) }}>
                DataAgent
            </span>
        </span>
    );
}
