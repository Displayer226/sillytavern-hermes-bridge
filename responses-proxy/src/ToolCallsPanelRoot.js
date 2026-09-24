import React, { useCallback, useState } from 'react';
import ToolCallsPanel from './ToolCallsPanel';
import { WorkspaceProvider } from './components/WorkspaceContext';
import ErrorBoundary from './ErrorBoundary';
import VoiceCall from './VoiceCall';

function ToolCallsPanelRoot({ context }) {
    const [voiceOpen, setVoiceOpen] = useState(false);
    const [voiceActive, setVoiceActive] = useState(false);
    const openVoice = useCallback(() => {
        if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
        setVoiceOpen(true);
    }, []);

    return (
        <React.StrictMode>
            <WorkspaceProvider>
                <ErrorBoundary>
                    <ToolCallsPanel
                        context={context}
                        voiceOpen={voiceOpen}
                        voiceActive={voiceActive}
                        onOpenVoice={openVoice}
                    />
                    <VoiceCall
                        open={voiceOpen}
                        onOpenChange={setVoiceOpen}
                        onActiveChange={setVoiceActive}
                    />
                </ErrorBoundary>
            </WorkspaceProvider>
        </React.StrictMode>
    );
}

export default ToolCallsPanelRoot;
