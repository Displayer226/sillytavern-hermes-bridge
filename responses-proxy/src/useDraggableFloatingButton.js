import { useCallback, useEffect, useRef, useState } from 'react';

const DRAG_THRESHOLD = 6;
const TOUCH_DRAG_THRESHOLD = 12;
const VIEWPORT_MARGIN = 8;
const KEYBOARD_MIN_HEIGHT = 120;
const SEND_FORM_GAP = 12;
const FLOATING_BUTTON_GAP = 8;

const registeredButtons = new Set();
let viewportBaseline = null;
let layoutFrame = 0;
let captureFrame = 0;
let listeningForViewport = false;
let nextRegistrationOrder = 0;

function viewportRect() {
    const viewport = window.visualViewport;
    return {
        left: viewport?.offsetLeft || 0,
        top: viewport?.offsetTop || 0,
        width: viewport?.width || window.innerWidth,
        height: viewport?.height || window.innerHeight,
    };
}

function clampPosition(position, element, maximumY = Infinity) {
    const viewport = viewportRect();
    const width = element?.offsetWidth || 48;
    const height = element?.offsetHeight || 48;
    const minX = viewport.left + VIEWPORT_MARGIN;
    const minY = viewport.top + VIEWPORT_MARGIN;
    const maxX = Math.max(minX, viewport.left + viewport.width - width - VIEWPORT_MARGIN);
    const maxY = Math.max(minY, Math.min(
        viewport.top + viewport.height - height - VIEWPORT_MARGIN,
        maximumY,
    ));
    return {
        x: Math.min(Math.max(position.x, minX), maxX),
        y: Math.min(Math.max(position.y, minY), maxY),
    };
}

function visibleSendFormTop(viewport) {
    const viewportBottom = viewport.top + viewport.height;
    const elements = ['#form_sheld', '#send_form']
        .map((selector) => document.querySelector(selector))
        .filter(Boolean);
    const tops = elements.flatMap((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const visible = style.display !== 'none'
            && style.visibility !== 'hidden'
            && Number(style.opacity || 1) !== 0
            && rect.width > 0
            && rect.height > 0
            && rect.bottom >= viewportBottom - 24
            && rect.top < viewportBottom;
        return visible ? [rect.top] : [];
    });
    return tops.length ? Math.min(...tops) : null;
}

function horizontallyOverlap(left, right) {
    return left.x < right.x + right.width + FLOATING_BUTTON_GAP
        && right.x < left.x + left.width + FLOATING_BUTTON_GAP;
}

function captureRestingRects() {
    window.cancelAnimationFrame(captureFrame);
    captureFrame = window.requestAnimationFrame(() => {
        for (const entry of registeredButtons) {
            const element = entry.elementRef.current;
            if (!element) continue;
            const rect = element.getBoundingClientRect();
            entry.restingRect = { x: rect.left, y: rect.top };
        }
    });
}

function layoutFloatingButtons() {
    window.cancelAnimationFrame(layoutFrame);
    layoutFrame = window.requestAnimationFrame(() => {
        const viewport = viewportRect();
        if (!viewportBaseline) viewportBaseline = { width: viewport.width, height: viewport.height };

        const widthChanged = Math.abs(viewport.width - viewportBaseline.width) > 80;
        if (widthChanged) viewportBaseline = { width: viewport.width, height: viewport.height };
        const keyboardThreshold = Math.max(KEYBOARD_MIN_HEIGHT, viewportBaseline.height * 0.18);
        const keyboardOpen = !widthChanged && viewport.height < viewportBaseline.height - keyboardThreshold;

        for (const entry of registeredButtons) entry.keyboardOpenRef.current = keyboardOpen;

        if (!keyboardOpen) {
            if (viewport.height > viewportBaseline.height) viewportBaseline.height = viewport.height;
            for (const entry of registeredButtons) {
                entry.setTemporaryPosition(null);
                entry.setPosition((current) => current
                    ? clampPosition(current, entry.elementRef.current)
                    : current);
            }
            // Wait for React to remove temporary inline coordinates before
            // remembering the CSS/default resting positions again.
            window.cancelAnimationFrame(captureFrame);
            captureFrame = window.requestAnimationFrame(captureRestingRects);
            return;
        }

        const sendFormTop = visibleSendFormTop(viewport);
        const items = [...registeredButtons].flatMap((entry) => {
            const element = entry.elementRef.current;
            if (!element || element.getClientRects().length === 0) return [];
            const rect = element.getBoundingClientRect();
            const resting = entry.restingPositionRef.current || entry.restingRect || {
                x: rect.left,
                y: rect.top,
            };
            const maximumY = sendFormTop === null
                ? Infinity
                : sendFormTop - element.offsetHeight - SEND_FORM_GAP;
            const desired = clampPosition(resting, element, maximumY);
            return [{
                entry,
                resting,
                desired,
                maximumY,
                width: element.offsetWidth || 48,
                height: element.offsetHeight || 48,
            }];
        }).sort((left, right) => left.resting.y - right.resting.y || left.entry.order - right.entry.order);

        // Work from bottom to top. Buttons in the same horizontal column keep
        // their original vertical order and are pushed above already-placed peers.
        const placed = [];
        for (let index = items.length - 1; index >= 0; index--) {
            const item = items[index];
            let y = item.desired.y;
            const horizontalPosition = { ...item.desired, width: item.width };
            for (const lower of placed) {
                if (horizontallyOverlap(horizontalPosition, lower)) {
                    y = Math.min(y, lower.y - item.height - FLOATING_BUTTON_GAP);
                }
            }
            const position = clampPosition(
                { x: item.desired.x, y },
                item.entry.elementRef.current,
                item.maximumY,
            );
            placed.push({ ...item.desired, ...position, width: item.width, height: item.height });
            item.entry.setTemporaryPosition(position);
        }
    });
}

function addViewportListeners() {
    if (listeningForViewport) return;
    listeningForViewport = true;
    viewportBaseline = viewportRect();
    window.addEventListener('resize', layoutFloatingButtons);
    window.visualViewport?.addEventListener('resize', layoutFloatingButtons);
    window.visualViewport?.addEventListener('scroll', layoutFloatingButtons);
}

function removeViewportListeners() {
    if (!listeningForViewport || registeredButtons.size) return;
    listeningForViewport = false;
    window.cancelAnimationFrame(layoutFrame);
    window.cancelAnimationFrame(captureFrame);
    window.removeEventListener('resize', layoutFloatingButtons);
    window.visualViewport?.removeEventListener('resize', layoutFloatingButtons);
    window.visualViewport?.removeEventListener('scroll', layoutFloatingButtons);
    viewportBaseline = null;
    nextRegistrationOrder = 0;
}

function readPosition(storageKey) {
    try {
        const value = JSON.parse(localStorage.getItem(storageKey) || 'null');
        if (Number.isFinite(value?.x) && Number.isFinite(value?.y)) return value;
    } catch (error) {
        console.warn('[Hermes Bridge] Could not restore floating button position:', error);
    }
    return null;
}

function savePosition(storageKey, position) {
    try {
        localStorage.setItem(storageKey, JSON.stringify(position));
    } catch (error) {
        console.warn('[Hermes Bridge] Could not save floating button position:', error);
    }
}

/**
 * Adds mouse, touch and pen dragging to a fixed/absolute floating control.
 * A short pointer movement remains a normal click; dragged positions persist.
 */
export default function useDraggableFloatingButton(storageKey, onActivate) {
    const elementRef = useRef(null);
    const dragRef = useRef(null);
    const suppressClickUntilRef = useRef(0);
    const restingPositionRef = useRef(null);
    const keyboardOpenRef = useRef(false);
    const [position, setPosition] = useState(null);
    const [temporaryPosition, setTemporaryPosition] = useState(null);
    const [dragging, setDragging] = useState(false);
    const displayedPosition = temporaryPosition || position;

    useEffect(() => {
        restingPositionRef.current = position;
    }, [position]);

    useEffect(() => {
        const stored = readPosition(storageKey);
        if (stored) setPosition(clampPosition(stored, elementRef.current));
    }, [storageKey]);

    useEffect(() => {
        const element = elementRef.current;
        const rect = element?.getBoundingClientRect();
        if (element && rect) {
            setPosition((current) => current || clampPosition({ x: rect.left, y: rect.top }, element));
        }
        const entry = {
            elementRef,
            keyboardOpenRef,
            restingPositionRef,
            restingRect: rect ? { x: rect.left, y: rect.top } : null,
            setPosition,
            setTemporaryPosition,
            order: nextRegistrationOrder++,
        };
        registeredButtons.add(entry);
        addViewportListeners();
        captureRestingRects();
        return () => {
            registeredButtons.delete(entry);
            removeViewportListeners();
        };
    }, []);

    const onPointerDown = useCallback((event) => {
        if (event.button !== undefined && event.button !== 0) return;
        const element = elementRef.current;
        if (!element) return;
        // A fresh press is a new intentional interaction. The suppression
        // window only belongs to the synthetic click from the previous press.
        suppressClickUntilRef.current = 0;
        const rect = element.getBoundingClientRect();
        dragRef.current = {
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            originX: rect.left,
            originY: rect.top,
            pointerType: event.pointerType,
            moved: false,
        };
        if (event.pointerType === 'mouse') element.setPointerCapture?.(event.pointerId);
    }, []);

    const onPointerMove = useCallback((event) => {
        const drag = dragRef.current;
        if (!drag || drag.pointerId !== event.pointerId) return;
        const dx = event.clientX - drag.startX;
        const dy = event.clientY - drag.startY;
        const threshold = drag.pointerType === 'touch' ? TOUCH_DRAG_THRESHOLD : DRAG_THRESHOLD;
        if (!drag.moved && Math.hypot(dx, dy) < threshold) return;
        if (!drag.moved) {
            drag.moved = true;
            elementRef.current?.setPointerCapture?.(event.pointerId);
        }
        suppressClickUntilRef.current = Infinity;
        setDragging(true);
        event.preventDefault();
        const nextPosition = clampPosition({ x: drag.originX + dx, y: drag.originY + dy }, elementRef.current);
        if (keyboardOpenRef.current) setTemporaryPosition(nextPosition);
        else setPosition(nextPosition);
    }, []);

    const finishDrag = useCallback((event) => {
        const drag = dragRef.current;
        if (!drag || drag.pointerId !== event.pointerId) return;
        dragRef.current = null;
        elementRef.current?.releasePointerCapture?.(event.pointerId);
        setDragging(false);
        if (drag.moved) {
            if (!keyboardOpenRef.current) {
                setPosition((current) => {
                    if (current) savePosition(storageKey, current);
                    return current;
                });
            }
            suppressClickUntilRef.current = Date.now() + 700;
        }
    }, [storageKey]);

    const onClick = useCallback((event) => {
        if (Date.now() < suppressClickUntilRef.current) {
            event.preventDefault();
            event.stopPropagation();
            return;
        }
        onActivate();
    }, [onActivate]);

    return {
        ref: elementRef,
        className: [
            'rp-draggable-floating-btn',
            dragging ? 'rp-is-dragging' : '',
            displayedPosition ? 'rp-has-custom-position' : '',
        ].filter(Boolean).join(' '),
        style: displayedPosition ? {
            left: `${displayedPosition.x}px`,
            top: `${displayedPosition.y}px`,
            right: 'auto',
            bottom: 'auto',
        } : undefined,
        onPointerDown,
        onPointerMove,
        onPointerUp: finishDrag,
        onPointerCancel: finishDrag,
        onClick,
    };
}
