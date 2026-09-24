import React, { useState, useEffect } from 'react';

const ANSI_ESCAPE_RE = /(?:\u001B\][\s\S]*?(?:\u0007|\u001B\\)|\u001B\[[0-?]*[ -/]*[@-~]|\u001B[@-Z\\-_])/g;

function stripAnsi(value) {
    return String(value ?? '').replace(ANSI_ESCAPE_RE, '');
}

function repairJoinedDiffLine(line) {
    const sign = line[0];
    if (sign !== '-') return [line];

    const indent = line.match(/^-[\t ]*/)?.[0]?.slice(1) || '';
    if (!indent) return [line];

    const needle = `+${indent}`;
    let splitAt = line.indexOf(needle, 1 + indent.length);
    while (splitAt !== -1) {
        const before = line.slice(0, splitAt);
        const after = line.slice(splitAt);
        const beforeTrimmed = before.trimEnd();
        const afterBody = after.slice(1 + indent.length);

        if (afterBody.trim() && /[\]\)\}"'`;]$/.test(beforeTrimmed)) {
            return [before, after];
        }

        splitAt = line.indexOf(needle, splitAt + 1);
    }

    return [line];
}

function normalizeDiffLines(diffText) {
    const cleanText = stripAnsi(diffText)
        .replace(/\r\n?/g, '\n')
        .replace(/\u0000/g, '');

    const lines = cleanText.split('\n');
    const result = [];
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].replace(/[ \t]+$/g, '');
        if (/^\s*(?:[|┊]\s*)?review diff\s*$/i.test(line)) {
            continue;
        }
        const repaired = repairJoinedDiffLine(line);
        if (Array.isArray(repaired)) {
            for (let j = 0; j < repaired.length; j++) {
                result.push(repaired[j]);
            }
        } else {
            result.push(repaired);
        }
    }
    return result;
}

function classifyDiffLine(line) {
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff --git') || /^index\s+[0-9a-f]/i.test(line)) {
        return { className: 'rp-diff-line-header', marker: '', content: line };
    }

    if (/^a\/+.+\s(?:->|→)\s+b\/+/.test(line)) {
        return { className: 'rp-diff-line-header', marker: '', content: line.replace(/^a\/+/, 'a/').replace(/\s+→\s+b\/+/, ' -> b/') };
    }

    if (line.startsWith('@@')) {
        return { className: 'rp-diff-line-hunk', marker: '', content: line };
    }

    if (line.startsWith('+')) {
        return { className: 'rp-diff-line-add', marker: '+', content: line.slice(1) || ' ' };
    }

    if (line.startsWith('-')) {
        return { className: 'rp-diff-line-del', marker: '-', content: line.slice(1) || ' ' };
    }

    if (line.startsWith('\\ No newline')) {
        return { className: 'rp-diff-line-note', marker: '', content: line };
    }

    if (line.startsWith(' ')) {
        return { className: 'rp-diff-line-normal', marker: '', content: line.slice(1) || ' ' };
    }

    return { className: 'rp-diff-line-normal', marker: '', content: line || ' ' };
}

function pathBasename(path) {
    return String(path || '').split(/[\\/]/).filter(Boolean).pop() || String(path || '');
}

function cleanDiffPath(path) {
    return String(path || '')
        .trim()
        .replace(/^['"]|['"]$/g, '')
        .replace(/\\/g, '/');
}

function pathFromDiffText(diffText) {
    const lines = normalizeDiffLines(diffText || '');
    let fallbackPath = '';

    for (const line of lines) {
        const patchFileMatch = line.match(/^\*\*\*\s+(?:Update|Add|Delete) File:\s+(.+)$/);
        if (patchFileMatch?.[1]) return cleanDiffPath(patchFileMatch[1]);

        const patchMoveMatch = line.match(/^\*\*\*\s+Move to:\s+(.+)$/);
        if (patchMoveMatch?.[1]) return cleanDiffPath(patchMoveMatch[1]);

        const arrowMatch = line.match(/\s(?:->|→)\s+b\/+(.+)$/);
        if (arrowMatch?.[1]) return cleanDiffPath(arrowMatch[1]);

        const plusMatch = line.match(/^\+\+\+\s+b\/(.+)$/);
        if (plusMatch?.[1] && plusMatch[1] !== '/dev/null') return cleanDiffPath(plusMatch[1]);

        const minusMatch = line.match(/^---\s+a\/(.+)$/);
        if (minusMatch?.[1] && minusMatch[1] !== '/dev/null' && !fallbackPath) {
            fallbackPath = cleanDiffPath(minusMatch[1]);
        }
    }
    return fallbackPath;
}

const DiffRenderer = React.memo(function DiffRenderer({ diffText }) {
    const lines = normalizeDiffLines(diffText || '');
    if (lines.length === 0) return null;

    return (
        <div className="rp-diff-viewer">
            {lines.map((line, idx) => {
                const { className, marker, content } = classifyDiffLine(line);

                return (
                    <div key={idx} className={`rp-diff-line ${className}`}>
                        <span className="rp-diff-line-num">{idx + 1}</span>
                        <span className="rp-diff-line-marker">{marker}</span>
                        <span className="rp-diff-line-content">{content}</span>
                    </div>
                );
            })}
        </div>
    );
});

const DiffsTab = React.memo(function DiffsTab({ toolCalls, focusPath }) {
    const diffCalls = toolCalls.filter(call => call.inline_diff);
    const [activeDiffId, setActiveDiffId] = useState(null);

    useEffect(() => {
        if (diffCalls.length > 0) {
            if (!activeDiffId || !diffCalls.some(call => call.id === activeDiffId)) {
                setActiveDiffId(diffCalls[diffCalls.length - 1].id);
            }
        }
    }, [diffCalls, activeDiffId]);

    if (diffCalls.length === 0) {
        return (
            <div className="rp-empty-state">
                <i className="fa-solid fa-code-compare" style={{ color: '#475569' }}></i>
                <div style={{ fontWeight: 500, color: '#475569' }}>No code diffs yet</div>
                <div style={{ fontSize: '0.78rem', color: '#334155' }}>When the agent modifies files, interactive code diffs will appear here.</div>
            </div>
        );
    }

    const activeCall = diffCalls.find(call => call.id === activeDiffId) || diffCalls[diffCalls.length - 1];

    const getFilePath = (call) => {
        if (call.arguments) {
            try {
                const parsed = JSON.parse(call.arguments);
                const file = parsed.TargetFile || parsed.target_file || parsed.path || parsed.filepath || parsed.filename || parsed.file;
                if (file) return String(file);
            } catch (e) {}
        }
        return pathFromDiffText(call.inline_diff) || call.name || 'Modified File';
    };

    const getFilename = (call) => {
        const filePath = getFilePath(call);
        return pathBasename(filePath) || 'Modified File';
    };

    return (
        <div className="rp-diffs-container">
            <div className="rp-diffs-sidebar">
                <div className="rp-diffs-sidebar-title">Modified Files</div>
                <div className="rp-diffs-list">
                    {diffCalls.map((call, idx) => {
                        const filename = getFilename(call);
                        const isActive = call.id === activeCall.id;
                        return (
                            <button
                                key={call.id || idx}
                                className={`rp-diff-item-btn ${isActive ? 'active' : ''}`}
                                onClick={() => setActiveDiffId(call.id)}
                            >
                                <i className="fa-solid fa-file-code" style={{ marginRight: '6px', color: isActive ? '#38bdf8' : '#64748b' }}></i>
                                <span className="rp-diff-filename">{filename}</span>
                                <span className="rp-diff-time">{new Date(call.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                            </button>
                        );
                    })}
                </div>
            </div>
            <div className="rp-diff-content-panel">
                <div className="rp-diff-content-header">
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", overflow: "hidden" }}>
                        <span className="rp-diff-header-title" title={getFilePath(activeCall)} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {getFilePath(activeCall)}
                        </span >
                    </div >
                    <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                        <span className="rp-diff-header-tool">via {activeCall.name}</span>
                        {focusPath && (
                            <button className="rp-icon-btn" onClick={() => focusPath(getFilePath(activeCall))} title="Focus in workspace">
                                <i className="fa-solid fa-eye"></i>
                            </button>
                        )}
                    </div >
                </div >
                <div className="rp-diff-scroller">
                    <DiffRenderer diffText={activeCall.inline_diff} />
                </div>
            </div>
        </div>
    );
});

export { DiffRenderer, DiffsTab, normalizeDiffLines, classifyDiffLine, pathBasename, pathFromDiffText, cleanDiffPath };
