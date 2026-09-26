import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import ProjectsPage from './ProjectsPage.jsx';
import TrainingJobsPage from './TrainingJobsPage.jsx';
import ExecutionsPage from './ExecutionsPage.jsx';
import OutputsPage from './OutputsPage.jsx';

function App() {
  const [page, setPage] = useState('projects');
  if (page === 'training-jobs') return <TrainingJobsPage onNavigate={setPage} />;
  if (page === 'executions') return <ExecutionsPage onNavigate={setPage} />;
  if (page === 'outputs') return <OutputsPage onNavigate={setPage} />;
  return <ProjectsPage onNavigate={setPage} />;
}

createRoot(document.getElementById('root')).render(<App />);
