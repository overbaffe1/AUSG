import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import { WorkspaceProvider } from './workspace'
import App from './App'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <HashRouter><WorkspaceProvider>
      <App />
    </WorkspaceProvider></HashRouter>
  </React.StrictMode>,
)
