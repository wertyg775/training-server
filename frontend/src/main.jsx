import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import ProjectsPage from './ProjectsPage.jsx';
import TrainingJobsPage from './TrainingJobsPage.jsx';

function App() {
  const [page, setPage] = useState('projects');
  return page === 'training-jobs'
    ? <TrainingJobsPage onNavigate={setPage} />
    : <ProjectsPage onNavigate={setPage} />;
}

createRoot(document.getElementById('root')).render(<App />);
