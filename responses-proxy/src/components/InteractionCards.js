import React, { useState, useEffect } from 'react';

const ClarifyCard = React.memo(function ClarifyCard({ pendingClarify, onSubmit, resetKey = 0 }) {
    const currentQuestion = pendingClarify.questions?.[0] || pendingClarify;
    const choices = currentQuestion.choices || [];
    const isMultiSelect = Boolean(currentQuestion.multi_select);
    const [inputValue, setInputValue] = useState('');
    const [selectedChoices, setSelectedChoices] = useState([]);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [showOtherInput, setShowOtherInput] = useState(choices.length === 0);

    useEffect(() => {
        setInputValue('');
        setSelectedChoices([]);
        setIsSubmitting(false);
        setShowOtherInput(choices.length === 0);
    }, [pendingClarify.request_id, currentQuestion.qid, resetKey]);

    const submit = (answer) => {
        if (isSubmitting) return;
        setIsSubmitting(true);
        onSubmit(pendingClarify.rpc_id, answer, currentQuestion.qid);
    };

    const handleChoiceClick = (choice) => {
        if (choice === 'Other') {
            setShowOtherInput(true);
        } else if (isMultiSelect) {
            setSelectedChoices((previous) => (
                previous.includes(choice)
                    ? previous.filter((item) => item !== choice)
                    : [...previous, choice]
            ));
        } else {
            submit(choice);
        }
    };

    const handleSendInput = () => {
        const typed = inputValue.trim();
        if (isMultiSelect) {
            const answers = typed ? [...selectedChoices, typed] : selectedChoices;
            if (answers.length) submit(JSON.stringify(answers));
        } else if (typed) {
            submit(typed);
        }
    };

    return (
        <div className="rp-clarify-card">
            <div className="rp-clarify-title">
                <i className="fa-solid fa-circle-question"></i>
                Clarification Required
                {pendingClarify.questions?.length > 1 && (
                    <span>({pendingClarify.questions.length} remaining)</span>
                )}
            </div>
            <div className="rp-clarify-question">{currentQuestion.question}</div>

            {choices.length > 0 && (
                <div className="rp-clarify-choices">
                    {choices.map((choice) => (
                        <button
                            key={`${pendingClarify.rpc_id || pendingClarify.request_id || 'clarify'}-${choice}`}
                            className={`rp-clarify-choice-btn ${selectedChoices.includes(choice) ? 'selected' : ''}`}
                            disabled={isSubmitting}
                            onClick={() => handleChoiceClick(choice)}
                        >
                            {isMultiSelect && (
                                <i className={`fa-${selectedChoices.includes(choice) ? 'solid fa-square-check' : 'regular fa-square'}`}></i>
                            )}
                            {choice}
                        </button>
                    ))}
                    <button
                        className="rp-clarify-choice-btn"
                        style={{ borderStyle: 'dashed', opacity: 0.8 }}
                        disabled={isSubmitting}
                        onClick={() => handleChoiceClick('Other')}
                    >
                        Other (type your answer)...
                    </button>
                </div>
            )}

            {showOtherInput && (
                <div className="rp-clarify-input-container">
                    <input
                        type="text"
                        className="rp-clarify-input"
                        placeholder="Type your response..."
                        value={inputValue}
                        onChange={(e) => setInputValue(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') handleSendInput();
                        }}
                        autoFocus
                        disabled={isSubmitting}
                    />
                    <button className="rp-clarify-send-btn" disabled={isSubmitting} onClick={handleSendInput}>
                        Send
                    </button>
                </div>
            )}

            {isMultiSelect && !showOtherInput && (
                <button
                    className="rp-clarify-send-btn"
                    disabled={isSubmitting || selectedChoices.length === 0}
                    onClick={handleSendInput}
                >
                    Send selection
                </button>
            )}
        </div>
    );
});

const ApprovalCard = React.memo(function ApprovalCard({ approval, onSubmit, resetKey = 0 }) {
    const [isSubmitting, setIsSubmitting] = useState(false);
    useEffect(() => setIsSubmitting(false), [approval.rpc_id || approval.request_id, resetKey]);
    const submit = (choice) => {
        if (isSubmitting) return;
        setIsSubmitting(true);
        onSubmit(approval.rpc_id, choice);
    };
    const choices = approval.choices || ['once', 'session', 'always', 'deny'];
    return (
        <div className="rp-approval-card">
            <div className="rp-approval-title">
                <i className="fa-solid fa-triangle-exclamation"></i>
                Approval Required
            </div>
            <div className="rp-approval-desc">
                <strong>Dangerous action:</strong> {approval.description || 'Executing a restricted command'}
            </div>
            <div className="rp-approval-cmd" title="Click to copy command" onClick={() => {
                navigator.clipboard.writeText(approval.command);
                SillyTavern.toastr.info('Command copied to clipboard!');
            }} style={{ cursor: 'pointer' }}>
                {approval.command}
            </div>
            <div className="rp-approval-actions">
                {choices.includes('once') && <button className="rp-approval-btn once" disabled={isSubmitting} onClick={() => submit('once')} title="Allow this command once">Allow Once</button>}
                {choices.includes('session') && <button className="rp-approval-btn session" disabled={isSubmitting} onClick={() => submit('session')} title="Allow this pattern for this session">Allow Session</button>}
                {choices.includes('always') && <button className="rp-approval-btn always" disabled={isSubmitting} onClick={() => submit('always')} title="Allow this pattern permanently">Allow Always</button>}
                {choices.includes('deny') && <button className="rp-approval-btn deny" disabled={isSubmitting} onClick={() => submit('deny')} title="Deny execution">Deny</button>}
            </div>
        </div>
    );
});

const SudoPasswordCard = React.memo(function SudoPasswordCard({ pendingSudo, onSubmit, resetKey = 0 }) {
    const [password, setPassword] = useState('');
    const [isSubmitting, setIsSubmitting] = useState(false);

    useEffect(() => {
        setPassword('');
        setIsSubmitting(false);
    }, [pendingSudo?.rpc_id || pendingSudo?.request_id, resetKey]);

    const submitPassword = (value) => {
        if (isSubmitting) return;
        setIsSubmitting(true);
        onSubmit(pendingSudo.rpc_id, value);
        setPassword('');
    };

    return (
        <div className="rp-sudo-card">
            <div className="rp-sudo-title">
                <i className="fa-solid fa-key"></i>
                Sudo Password Required
            </div>
            <div className="rp-sudo-desc">
                Hermes needs your sudo password for this command. The password is cached by Hermes for this session only.
            </div>
            <div className="rp-sudo-input-row">
                <input
                    type="password"
                    className="rp-sudo-input"
                    placeholder="Password"
                    value={password}
                    autoComplete="off"
                    onChange={(e) => setPassword(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' && password) submitPassword(password);
                    }}
                    autoFocus
                />
                <button
                    className="rp-sudo-btn submit"
                    disabled={!password || isSubmitting}
                    onClick={() => submitPassword(password)}
                >
                    Submit
                </button>
            </div>
            <div className="rp-sudo-actions">
                <button
                    className="rp-sudo-btn skip"
                    disabled={isSubmitting}
                    onClick={() => submitPassword('')}
                >
                    Skip
                </button>
            </div>
        </div>
    );
});

export { ClarifyCard, ApprovalCard, SudoPasswordCard };
