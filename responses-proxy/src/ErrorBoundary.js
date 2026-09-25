import React from 'react';

class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }

    componentDidCatch(error, errorInfo) {
        console.error('[Hermes Bridge] Error caught by boundary:', error, errorInfo);
    }

    render() {
        if (this.state.hasError) {
            return (
                <div style={{ padding: '10px', color: 'red', background: '#fee', border: '1px solid red', borderRadius: '4px' }}>
                    <h4>Something went wrong in Hermes Bridge UI.</h4>
                    <pre style={{ fontSize: '11px', whiteSpace: 'pre-wrap' }}>{String(this.state.error)}</pre>
                </div>
            );
        }
        return this.props.children;
    }
}

export default ErrorBoundary;
