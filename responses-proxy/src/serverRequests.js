const SERVER_REQUEST_METHODS = new Set(['approval', 'clarify', 'sudo']);
const APPROVAL_CHOICES = new Set(['once', 'session', 'always', 'deny']);
const SERVER_REQUEST_ID = /^srq-[A-Za-z0-9_-]{1,64}$/;
const MAX_TEXT = 4096;
const MAX_QUESTIONS = 64;
const MAX_CHOICES = 64;

function boundedString(value, limit = MAX_TEXT) {
    return typeof value === 'string' && value.length <= limit ? value : null;
}

function normalizeApproval(params) {
    const requestId = boundedString(params.request_id, 256);
    const command = boundedString(params.command ?? '');
    const description = boundedString(params.description ?? '');
    if (!requestId || command === null || description === null) return null;

    const rawChoices = params.choices;
    const choices = rawChoices === undefined
        ? ['once', 'session', 'always', 'deny']
        : rawChoices;
    if (!Array.isArray(choices) || choices.length > MAX_CHOICES || choices.some((choice) => (
        typeof choice !== 'string' || !APPROVAL_CHOICES.has(choice)
    ))) return null;

    const normalized = {
        request_id: requestId,
        command,
        description,
        choices: [...new Set(choices)],
    };
    for (const key of ['allow_permanent', 'allow_session', 'smart_denied']) {
        if (params[key] !== undefined) {
            if (typeof params[key] !== 'boolean') return null;
            normalized[key] = params[key];
        }
    }
    if (params.tool_name !== undefined) {
        const toolName = boundedString(params.tool_name, 256);
        if (toolName === null) return null;
        normalized.tool_name = toolName;
    }
    return normalized;
}

function normalizeQuestion(question) {
    if (!question || typeof question !== 'object') return null;
    const qid = boundedString(question.qid, 256);
    const prompt = boundedString(question.question);
    if (!qid || prompt === null) return null;
    const normalized = { qid, question: prompt };
    if (question.choices !== undefined) {
        if (!Array.isArray(question.choices) || question.choices.length > MAX_CHOICES || question.choices.some((choice) => (
            typeof choice !== 'string' || choice.length > MAX_TEXT
        ))) return null;
        normalized.choices = [...question.choices];
    }
    if (question.multi_select !== undefined && typeof question.multi_select !== 'boolean') return null;
    normalized.multi_select = Boolean(question.multi_select);
    return normalized;
}

function normalizeClarify(params) {
    if (params.questions !== undefined) {
        if (!Array.isArray(params.questions) || params.questions.length === 0 || params.questions.length > MAX_QUESTIONS) {
            return null;
        }
        const questions = params.questions.map(normalizeQuestion);
        if (questions.some((question) => question === null)) return null;
        return { questions };
    }

    const question = boundedString(params.question);
    if (question === null) return null;
    const normalized = { question };
    if (params.choices !== undefined) {
        if (!Array.isArray(params.choices) || params.choices.length > MAX_CHOICES || params.choices.some((choice) => (
            typeof choice !== 'string' || choice.length > MAX_TEXT
        ))) return null;
        normalized.choices = [...params.choices];
    }
    if (params.multi_select !== undefined && typeof params.multi_select !== 'boolean') return null;
    normalized.multi_select = Boolean(params.multi_select);
    return normalized;
}

/**
 * Normalize the browser-facing server_request envelope. The stable ST id is
 * kept at the envelope level; Hermes' TUI id is never accepted here.
 */
export function normalizeServerRequest(message) {
    if (!message || message.type !== 'server_request') return null;
    if (typeof message.session_id !== 'string' || message.session_id.length === 0 || message.session_id.length > 256) return null;
    if (typeof message.rpc_id !== 'string' || !SERVER_REQUEST_ID.test(message.rpc_id)) return null;
    if (!SERVER_REQUEST_METHODS.has(message.method) || !message.params || typeof message.params !== 'object' || Array.isArray(message.params)) return null;

    let params;
    if (message.method === 'approval') params = normalizeApproval(message.params);
    else if (message.method === 'clarify') params = normalizeClarify(message.params);
    else params = {};
    if (params === null) return null;

    return {
        type: 'server_request',
        session_id: message.session_id,
        rpc_id: message.rpc_id,
        method: message.method,
        params,
    };
}

export function upsertServerRequest(requests, message) {
    const normalized = normalizeServerRequest(message);
    if (!normalized) return requests;
    const previous = Array.isArray(requests) ? requests : [];
    const index = previous.findIndex((request) => request.rpc_id === normalized.rpc_id);
    if (index < 0) return [...previous, normalized];
    const next = [...previous];
    next[index] = normalized;
    return next;
}

export function removeServerRequest(requests, message) {
    const previous = Array.isArray(requests) ? requests : [];
    if (!message || typeof message.rpc_id !== 'string') return previous;
    return previous.filter((request) => {
        if (request.rpc_id !== message.rpc_id) return true;
        if (message.session_id && request.session_id !== message.session_id) return true;
        if (message.method && request.method !== message.method) return true;
        return false;
    });
}

export function normalizeServerRequestSnapshot(items) {
    return (Array.isArray(items) ? items : []).reduce(
        (requests, item) => upsertServerRequest(requests, item),
        [],
    );
}

export function serverRequestKey(request) {
    return request?.rpc_id || '';
}

export function classifyServerRequestError(message) {
    if (!message || message.type !== 'server_request_error') return null;
    if (typeof message.session_id !== 'string' || typeof message.rpc_id !== 'string' || typeof message.method !== 'string') return null;
    return message.status === 'rejected' ? 'retry' : 'remove';
}

export function buildServerRequestResponse(sessionId, request, result) {
    if (!sessionId || !request?.rpc_id || !request?.method) return null;
    return {
        type: 'server_request_response',
        session_id: sessionId,
        rpc_id: request.rpc_id,
        method: request.method,
        result,
    };
}

export function buildClarifyLock(sessionId, request, questionId, answer) {
    if (!sessionId || !request?.rpc_id || !questionId) return null;
    return {
        type: 'server_request_clarify_lock',
        session_id: sessionId,
        rpc_id: request.rpc_id,
        question_id: questionId,
        answer,
    };
}
