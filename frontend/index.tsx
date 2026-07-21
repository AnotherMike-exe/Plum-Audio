import './src/index.css';
import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
// Phase-3 port: the entry renders the mesh slice (MeshApp) while App.tsx (the vendored Snapcast
// GUI) is ported incrementally. Swap back to `App` once the full port lands.
import MeshApp from './MeshApp';

const rootElement = document.getElementById('root');
if (!rootElement) {
    throw new Error("Could not find root element to mount to");
}

const root = ReactDOM.createRoot(rootElement);
root.render(
    <React.StrictMode>
        <BrowserRouter>
            <MeshApp/>
        </BrowserRouter>
    </React.StrictMode>
);
