import React, { createContext, useContext, useState, useCallback } from 'react';

const WorkspaceContext = createContext(null);

export function WorkspaceProvider({ children }) {
    const [focusedPath, setFocusedPath] = useState(null);

    const focusPath = useCallback((path) => {
        setFocusedPath(path);
    }, []);

    const clearFocus = useCallback(() => {
        setFocusedPath(null);
    }, []);

    return (
        <WorkspaceContext.Provider value={{ focusedPath, focusPath, clearFocus }}>
            {children}
        </WorkspaceContext.Provider>
    );
}

export function useWorkspaceFocus() {
    const context = useContext(WorkspaceContext);
    if (!context) {
        throw new Error('useWorkspaceFocus must be used within a WorkspaceProvider');
    }
    return context;
}
