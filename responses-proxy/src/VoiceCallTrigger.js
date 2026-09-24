import React from 'react';
import useDraggableFloatingButton from './useDraggableFloatingButton';

export default function VoiceCallTrigger({ active = false, open = false, onActivate }) {
    const floatingButton = useDraggableFloatingButton('responses-proxy-floating-voice', onActivate);

    return <>
        <style>{`
            .rp-voice-floating-btn {
                position: absolute;
                right: 20px;
                bottom: 145px;
                z-index: 10001;
                width: 48px;
                height: 48px;
                max-width: none;
                padding: 0;
                box-sizing: border-box;
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 50%;
                background: linear-gradient(135deg, #22604d, #33866b);
                color: #ffffff;
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.4);
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                pointer-events: auto;
                touch-action: none;
                user-select: none;
                -webkit-user-select: none;
                transition: left 220ms cubic-bezier(0.22, 1, 0.36, 1), top 220ms cubic-bezier(0.22, 1, 0.36, 1), right 220ms cubic-bezier(0.22, 1, 0.36, 1), bottom 220ms cubic-bezier(0.22, 1, 0.36, 1), transform 160ms ease, box-shadow 160ms ease;
            }
            .rp-voice-floating-btn[data-active="true"] {
                color: #67e8b2;
                background: #16392f;
            }
            .rp-voice-floating-btn.rp-is-dragging {
                cursor: grabbing;
                transition: none;
                transform: none;
            }
            .rp-voice-floating-btn:focus-visible {
                outline: 3px solid #8ac9ff;
                outline-offset: 3px;
            }
            @media (prefers-reduced-motion: reduce) {
                .rp-voice-floating-btn { transition: none; }
            }
            @media (max-width: 1000px) {
                .rp-voice-floating-btn {
                    width: 44px;
                    height: 44px;
                    z-index: 10001;
                }
                .rp-voice-floating-btn:not(.rp-has-custom-position) {
                    right: 10px;
                    bottom: 135px;
                }
            }
        `}</style>
        <button
            type="button"
            {...floatingButton}
            className={`rp-voice-floating-btn ${floatingButton.className}`}
            data-active={active}
            title={active ? 'Afficher l’appel en cours' : 'Appeler Hermes'}
            aria-label={active ? 'Afficher l’appel en cours' : 'Appeler Hermes'}
            aria-expanded={open}
        >
            <i className="fa-solid fa-phone" aria-hidden="true" />
        </button>
    </>;
}
