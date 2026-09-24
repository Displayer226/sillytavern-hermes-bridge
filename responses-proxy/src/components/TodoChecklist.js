import React, { useMemo } from 'react';

const TODO_STATUS_LABELS = {
    pending: 'Pending',
    in_progress: 'In progress',
    completed: 'Completed',
    cancelled: 'Cancelled',
};

function normalizeTodoStatus(status) {
    const normalized = String(status || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
    if (['complete', 'completed', 'done', 'success', 'succeeded'].includes(normalized)) return 'completed';
    if (['inprogress', 'in_progress', 'running', 'active', 'started'].includes(normalized)) return 'in_progress';
    if (['cancel', 'cancelled', 'canceled', 'skipped', 'abandoned'].includes(normalized)) return 'cancelled';
    if (['open', 'opened', 'todo', 'planned', 'pending'].includes(normalized)) return 'pending';
    return normalized;
}

function todoStatusLabel(status) {
    const normalized = normalizeTodoStatus(status);
    return TODO_STATUS_LABELS[normalized] || (normalized ? normalized.replace(/_/g, ' ') : 'Pending');
}

function parseTodos(todosData) {
    if (todosData === undefined || todosData === null) return [];

    if (typeof todosData === 'string') {
        const trimmed = todosData.trim();
        if (/^[\[{]/.test(trimmed)) {
            try {
                const parsed = JSON.parse(trimmed);
                return parseTodos(Array.isArray(parsed) ? parsed : (parsed ? parsed.todos : []));
            } catch (e) {
                // Fall through to line-based parsing below.
            }
        }
    }

    if (typeof todosData === 'object' && !Array.isArray(todosData) && Array.isArray(todosData.todos)) {
        return parseTodos(todosData.todos);
    }

    const parseTodosFromString = (str) => {
        if (typeof str !== 'string') return [];
        const lines = str.split('\n');
        const list = [];
        for (let line of lines) {
            line = line.trim();
            if (!line) continue;
            const match = line.match(/^\[([ xX✓])\]\s*(.*)$/);
            if (match) {
                const completed = match[1].toLowerCase() === 'x' || match[1] === '✓';
                list.push({ text: match[2].trim(), status: completed ? 'completed' : 'pending', completed });
            } else {
                list.push({ text: line, status: 'pending', completed: false });
            }
        }
        return list;
    };

    if (Array.isArray(todosData)) {
        return todosData.map(item => {
            if (typeof item === 'string') {
                const match = item.match(/^\[([ xX✓])\]\s*(.*)$/);
                if (match) {
                    const completed = match[1].toLowerCase() === 'x' || match[1] === '✓';
                    return { text: match[2].trim(), status: completed ? 'completed' : 'pending', completed };
                }
                return { text: item, status: 'pending', completed: false };
            }
            if (typeof item === 'object' && item !== null) {
                const fallbackStatus = item.cancelled || item.canceled ? 'cancelled' : (item.completed || item.done || item.checked ? 'completed' : 'pending');
                const status = normalizeTodoStatus(item.status || fallbackStatus);
                return {
                    text: item.content || item.text || item.title || item.name || item.id || '',
                    status,
                    completed: Boolean(
                        item.completed ||
                        item.done ||
                        item.checked ||
                        status === 'completed' ||
                        status === 'done'
                    ),
                };
            }
            return { text: String(item), status: 'pending', completed: false };
        });
    }
    if (typeof todosData === 'string') {
        return parseTodosFromString(todosData);
    }
    return [];
}

function parseJsonValue(value) {
    if (typeof value !== 'string') return value;
    const trimmed = value.trim();
    if (!/^[\[{]/.test(trimmed)) return undefined;
    try {
        return JSON.parse(trimmed);
    } catch (e) {
        return undefined;
    }
}

function isTodoToolCall(call) {
    return /todo/i.test(String(call?.name || ''));
}

function todosDataFromValue(value, allowList = false) {
    const parsed = parseJsonValue(value);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && Array.isArray(parsed.todos)) {
        return parsed.todos;
    }
    if (allowList && Array.isArray(parsed)) {
        return parsed;
    }
    return undefined;
}

function todosDataFromCall(call) {
    if (!call || typeof call !== 'object') return undefined;
    if (Object.prototype.hasOwnProperty.call(call, 'todos')) {
        return call.todos;
    }

    const outputTodos = todosDataFromValue(call.output);
    if (outputTodos !== undefined) return outputTodos;

    const resultTextTodos = todosDataFromValue(call.result_text);
    if (resultTextTodos !== undefined) return resultTextTodos;

    if (isTodoToolCall(call)) {
        return todosDataFromValue(call.arguments, true);
    }
    return undefined;
}

function hasTodoSnapshot(call) {
    return todosDataFromCall(call) !== undefined;
}

function toolCallTimestampMs(call) {
    const parsed = Date.parse(call?.updated_at || call?.timestamp || '');
    return Number.isFinite(parsed) ? parsed : 0;
}

function latestTodoSnapshot(toolCalls) {
    let latest = null;
    for (let i = 0; i < toolCalls.length; i++) {
        const call = toolCalls[i];
        const todosData = todosDataFromCall(call);
        if (todosData === undefined) continue;
        const timestamp = toolCallTimestampMs(call);
        if (!latest || timestamp > latest.timestamp || (timestamp === latest.timestamp && i > latest.index)) {
            latest = { todosData, timestamp, index: i };
        }
    }
    return latest;
}

function latestTodoItems(toolCalls) {
    const latest = latestTodoSnapshot(toolCalls);
    return latest ? parseTodos(latest.todosData) : [];
}

function TodoStatusIcon({ item }) {
    if (item.completed || item.status === 'completed') {
        return <i className="fa-solid fa-circle-check" style={{ color: '#10b981' }}></i>;
    }
    if (item.status === 'in_progress') {
        return <i className="fa-solid fa-circle-dot" style={{ color: '#38bdf8' }}></i>;
    }
    if (item.status === 'cancelled') {
        return <i className="fa-solid fa-circle-xmark" style={{ color: '#f97316' }}></i>;
    }
    if (item.status === 'pending') {
        return <i className="fa-regular fa-clock" style={{ color: '#f59e0b' }}></i>;
    }
    return <i className="fa-regular fa-circle" style={{ color: '#64748b' }}></i>;
}

const TodoChecklist = React.memo(function TodoChecklist({ toolCalls }) {
    const items = useMemo(() => latestTodoItems(toolCalls), [toolCalls]);
    const hasTodos = useMemo(() => latestTodoSnapshot(toolCalls) !== null, [toolCalls]);

    if (items.length === 0 && !hasTodos) {
        return (
            <div className="rp-empty-state">
                <i className="fa-solid fa-list-check" style={{ color: '#475569' }}></i>
                <div style={{ fontWeight: 500, color: '#475569' }}>No planning steps yet</div>
                <div style={{ fontSize: '0.78rem', color: '#334155' }}>When the agent outlines its plan, the checklist will appear here.</div>
            </div>
        );
    }

    if (items.length === 0) {
        return (
            <div className="rp-empty-state">
                <i className="fa-solid fa-list-check" style={{ color: '#475569' }}></i>
                <div style={{ fontWeight: 500, color: '#475569' }}>Empty checklist</div>
                <div style={{ fontSize: '0.78rem', color: '#334155' }}>The agent's plan has no steps listed.</div>
            </div>
        );
    }

    const completedCount = items.filter(i => i.completed).length;
    const totalCount = items.length;
    const progressPercent = totalCount > 0 ? Math.round((completedCount / totalCount) * 100) : 0;

    return (
        <div className="rp-todo-container">
            <div className="rp-todo-header">
                <div className="rp-todo-progress-label">Plan Execution Progress</div>
                <div className="rp-todo-progress-value">{completedCount} / {totalCount} ({progressPercent}%)</div>
            </div>
            <div className="rp-todo-progress-bar">
                <div className="rp-todo-progress-fill" style={{ width: `${progressPercent}%` }} />
            </div>
            <div className="rp-todo-list">
                {items.map((item, idx) => (
                    <div key={idx} className={`rp-todo-item ${item.completed ? 'completed' : ''} ${item.status ? `status-${item.status}` : ''}`}>
                        <div className="rp-todo-item-checkbox">
                            <TodoStatusIcon item={item} />
                        </div>
                        <span className="rp-todo-item-text">{item.text}</span>
                        <span className={`rp-todo-status-badge status-${item.status || 'pending'}`}>
                            {todoStatusLabel(item.status)}
                        </span>
                    </div>
                ))}
            </div>
        </div>
    );
});

export { TodoChecklist, hasTodoSnapshot };
