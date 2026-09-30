import { createRoot } from 'react-dom/client';
import { App, ErrorBoundary } from './App';
import { OwnerApi, takeBootstrap } from './api';
import './styles.css';

// Consume and remove the fragment before any authentication request or UI rendering.
const bootstrap = takeBootstrap();
const api = new OwnerApi(window.location.origin);
createRoot(document.getElementById('root')!).render(<ErrorBoundary><App api={api} bootstrap={bootstrap} /></ErrorBoundary>);
