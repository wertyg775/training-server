import React, { useEffect, useId, useRef, useState } from 'react';
import { listProjectFiles, readProjectFile } from './api.js';
import TrainingForm from './TrainingForm.jsx';
import DatasetModal from './DatasetModal.jsx';

function Directory({ projectId, path = '', selectedFile, onSelectFile }) {
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setEntries(null);
    setError('');
    listProjectFiles(projectId, path, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setEntries(data.entries); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, [projectId, path, attempt]);

  if (error) return <div className="tree-message"><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></div>;
  if (entries === null) return <p className="tree-message" role="status">Loading files…</p>;
  if (!entries.length) return <p className="tree-message">Empty directory</p>;
  return <ul className="directory-list">
    {[...entries].sort((a, b) => Number(b.type === 'directory') - Number(a.type === 'directory') || a.name.localeCompare(b.name)).map((entry) => <DirectoryEntry key={entry.path} projectId={projectId} entry={entry} selectedFile={selectedFile} onSelectFile={onSelectFile} />)}
  </ul>;
}

function EntryIcon({ folder, expanded }) {
  return <svg className="entry-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
    {folder
      ? expanded
        ? <path d="M2 7V4h6l2 2h7v3M2 7h6l2 2h8l-3 7H3Z" />
        : <path d="M2 4h6l2 2h8v10H2Z" />
      : <><path d="M5 2h6l4 4v12H5Z" /><path d="M11 2v5h4" /></>}
  </svg>;
}

function DirectoryEntry({ projectId, entry, selectedFile, onSelectFile, initiallyExpanded = false }) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const [opened, setOpened] = useState(initiallyExpanded);
  const childrenId = useId();
  const isDirectory = entry.type === 'directory';
  useEffect(() => {
    if (entry.path && selectedFile?.startsWith(`${entry.path}/`)) { setExpanded(true); setOpened(true); }
  }, [entry.path, selectedFile]);
  return <li>
    {isDirectory ? <>
      <button className="directory-toggle" type="button" aria-expanded={expanded} aria-controls={childrenId} onClick={() => { setOpened(true); setExpanded((value) => !value); }} title={entry.path}>
        <span className="entry-chevron" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
        <EntryIcon folder expanded={expanded} />
        <span className="entry-name">{entry.name}/</span>
      </button>
      <div id={childrenId} hidden={!expanded}>{opened && <Directory projectId={projectId} path={entry.path} selectedFile={selectedFile} onSelectFile={onSelectFile} />}</div>
    </> : <button type="button" className="directory-file" title={entry.path} aria-pressed={selectedFile === entry.path} onClick={() => onSelectFile(entry.path)} disabled={entry.type !== 'file'}>
      <span className="entry-chevron" aria-hidden="true" />
      <EntryIcon />
      <span className="entry-name">{entry.name}</span>
      {entry.type !== 'file' && <span className="entry-type">{entry.type}</span>}
    </button>}
  </li>;
}

function FilePreview({ projectId, path }) {
  const [content, setContent] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setContent(null);
    setError('');
    readProjectFile(projectId, path, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setContent(data.content); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, [projectId, path, attempt]);
  if (error) return <div className="tree-message"><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></div>;
  if (content === null) return <p className="tree-message" role="status">Loading file…</p>;
  if (!content) return <p className="tree-message">This file is empty.</p>;
  return <pre className="file-content"><code>{content}</code></pre>;
}

export default function ProjectSidebar({ project, onClose }) {
  const [selectedFile, setSelectedFile] = useState(null);
  const [dataset, setDataset] = useState(null);
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [trainingBusy, setTrainingBusy] = useState(false);
  const manuallySelected = useRef(false);
  function selectFile(path) { manuallySelected.current = true; setSelectedFile(path); }
  useEffect(() => {
    const controller = new AbortController();
    async function choosePythonFile() {
      const directories = [''];
      let fallback = null;
      while (directories.length && !controller.signal.aborted && !manuallySelected.current) {
        const path = directories.shift();
        let entries;
        try { entries = (await listProjectFiles(project.id, path, controller.signal)).entries; }
        catch { continue; }
        entries.sort((a, b) => a.name.localeCompare(b.name));
        const files = entries.filter((entry) => entry.type === 'file');
        fallback ||= files[0]?.path;
        const python = files.find((entry) => entry.name === 'train.py') || files.find((entry) => entry.name.endsWith('.py'));
        if (python) {
          if (!controller.signal.aborted && !manuallySelected.current) setSelectedFile(python.path);
          return;
        }
        directories.push(...entries.filter((entry) => entry.type === 'directory').map((entry) => entry.path));
      }
      if (!controller.signal.aborted && !manuallySelected.current && fallback) setSelectedFile(fallback);
    }
    choosePythonFile();
    return () => controller.abort();
  }, [project.id]);
  const closeButton = useRef(null);
  useEffect(() => { closeButton.current?.focus(); }, []);

  return <aside id="project-sidebar" className="project-sidebar" aria-labelledby="project-sidebar-title" onKeyDown={(event) => {
    if (event.key === 'Escape' && !datasetOpen) {
      event.stopPropagation();
      onClose();
    }
  }}>
    <section className="project-tree-section" aria-label="Project directory">
      <header className="project-sidebar-header">
        <div>
          <h2 id="project-sidebar-title">{project.name}</h2>
          <p>Project directory</p>
        </div>
        <button ref={closeButton} className="sidebar-close" type="button" aria-label="Close project sidebar" onClick={onClose}>×</button>
      </header>
      <div className="directory-actions">
        <button type="button" disabled={trainingBusy} onClick={() => setDatasetOpen(true)}>Upload dataset</button>
        {dataset && <span title={dataset.target}>{dataset.name}</span>}
      </div>
      <nav className="project-tree" aria-label="Project files"><ul className="directory-list directory-root"><DirectoryEntry projectId={project.id} entry={{ name: project.name, path: '', type: 'directory' }} initiallyExpanded selectedFile={selectedFile} onSelectFile={selectFile} /></ul></nav>
    </section>
    <section className="project-file-preview" aria-label="File contents">
      <TrainingForm projectId={project.id} path={selectedFile} dataset={dataset} onBusyChange={setTrainingBusy} />
      {selectedFile
        ? <FilePreview key={selectedFile} projectId={project.id} path={selectedFile} />
        : <p className="tree-message">Select a file above to view its contents.</p>}
    </section>
    {datasetOpen && <DatasetModal projectId={project.id} onUploaded={setDataset} onClose={() => setDatasetOpen(false)} />}
  </aside>;
}
