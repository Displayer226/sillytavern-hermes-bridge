import React from 'react';

const CONSOLE_TOOL_RE = /\b(exec|terminal|shell|bash|zsh|powershell|cmd|command|run|python|node|npm|docker)\b/i;

function isConsoleToolCall(call) {
    return CONSOLE_TOOL_RE.test(call.name || '');
}

const TerminalConsole = React.memo(function TerminalConsole({ toolCalls }) {
    const consoleCalls = toolCalls.filter(isConsoleToolCall);

    const parseMaybeJson = (value) => {
        if (typeof value !== 'string') return value;
        const trimmed = value.trim();
        if (!trimmed || !/^[\[{]/.test(trimmed)) return value;
        try {
            return JSON.parse(trimmed);
        } catch (e) {
            return value;
        }
    };

    const valueToText = (value) => {
        if (value === null || value === undefined) return '';
        if (typeof value === 'string') return value;
        if (typeof value === 'number' || typeof value === 'boolean') return String(value);
        if (Array.isArray(value)) return value.map(valueToText).filter(Boolean).join('\n');
        if (typeof value === 'object') {
            return valueToText(value.text ?? value.content ?? value.output) || JSON.stringify(value, null, 2);
        }
        return String(value);
    };

    const parseCommand = (call) => {
        if (!call.arguments) return '';
        try {
            const parsed = JSON.parse(call.arguments);
            return parsed.command || parsed.cmd || parsed.script || parsed.code || parsed.input || call.arguments;
        } catch (e) {
            return call.arguments;
        }
    };

    const extractConsoleOutput = (call) => {
        const parsedOutput = parseMaybeJson(call.output);
        const parsedResult = parseMaybeJson(call.result_text);
        const stdout = valueToText(call.stdout ?? parsedResult?.stdout ?? parsedOutput?.stdout);
        const stderr = valueToText(call.stderr ?? parsedResult?.stderr ?? parsedOutput?.stderr);

        const parts = [];
        if (stdout) parts.push(stdout);
        if (stderr) parts.push(`${parts.length ? '[stderr]\n' : ''}${stderr}`);
        if (parts.length) return parts.join('\n');

        return valueToText(call.result_text ?? call.output ?? call.error);
    };

    const hasConsoleError = (call) => {
        const exitCode = Number(call.exit_code ?? call.returncode);
        return Boolean(call.error) || (Number.isFinite(exitCode) && exitCode !== 0);
    };

    if (consoleCalls.length === 0) {
        return (
            <div className="rp-empty-state">
                <i className="fa-solid fa-terminal" style={{ color: '#475569' }}></i>
                <div style={{ fontWeight: 500, color: '#475569' }}>No terminal outputs yet</div>
                <div style={{ fontSize: '0.78rem', color: '#334155' }}>When the agent executes shell commands, the console output will appear here.</div>
            </div>
        );
    }

    return (
        <div className="rp-console-container">
            {consoleCalls.map((call, idx) => {
                const cmd = parseCommand(call);
                const outputText = extractConsoleOutput(call);
                const hasError = hasConsoleError(call);
                return (
                    <div key={call.id || idx} className="rp-console-item">
                        <div className="rp-console-cmd-header">
                            <span className="rp-console-prompt">$</span>
                            <span className="rp-console-cmd-text" title={cmd}>{cmd}</span>
                            {call.status === 'running' && (
                                <i className="fa-solid fa-spinner fa-spin rp-console-status-icon text-info" />
                            )}
                            {call.status === 'completed' && !hasError && (
                                <i className="fa-solid fa-circle-check rp-console-status-icon text-success" />
                            )}
                            {(call.status === 'error' || hasError) && (
                                <i className="fa-solid fa-circle-xmark rp-console-status-icon text-danger" />
                            )}
                        </div>
                        {outputText && (
                            <pre className={`rp-console-output ${hasError ? 'rp-console-error' : ''}`}>
                                {outputText}
                            </pre>
                        )}
                    </div>
                );
            })}
        </div>
    );
});

export { TerminalConsole, isConsoleToolCall };
