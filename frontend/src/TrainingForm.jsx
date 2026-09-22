import React, { useEffect, useRef, useState } from 'react';
import { getTrainingJob, submitTraining, uploadDataset } from './api.js';

export default function TrainingForm({ projectId, path }) {
  const [epochs, setEpochs] = useState('1');
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState('');
  const [error, setError] = useState('');
  const [savedJob, setSavedJob] = useState(null);
  const [datasetFile, setDatasetFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const uploadedDataset = useRef(null);
  const datasetInput = useRef(null);
  const inFlight = useRef(false);
  const runnable = path?.endsWith('.py');

  useEffect(() => {
    if (!savedJob) return undefined;
    const controller = new AbortController();
    let timer;
    async function refresh() {
      let delay = 5000;
      try {
        const job = await getTrainingJob(projectId, savedJob.id, controller.signal);
        if (controller.signal.aborted) return;
        setSavedJob(job);
        if (!['queued', 'running'].includes(job.status)) {
          if (!job.dataset || job.dataset.deleted_at) return;
          delay = 60000;
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError(`Could not refresh job status: ${failure.message}`);
      }
      if (!controller.signal.aborted) timer = setTimeout(refresh, delay);
    }
    timer = setTimeout(refresh, 5000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [projectId, savedJob?.id]);

  async function submit(event) {
    event.preventDefault();
    if (inFlight.current || !runnable) return;
    const count = Number(epochs);
    if (!Number.isInteger(count) || count < 1 || count > 2147483647) {
      setError('Enter a positive whole number of epochs.');
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError('');
    setResult('');
    setSavedJob(null);
    try {
      if (datasetFile && (!uploadedDataset.current || new Date(uploadedDataset.current.expires_at) <= new Date())) {
        setUploading(true);
        uploadedDataset.current = await uploadDataset(projectId, datasetFile);
        setUploading(false);
      }
      const job = await submitTraining(projectId, path, count, uploadedDataset.current?.id);
      uploadedDataset.current = null;
      setDatasetFile(null);
      if (datasetInput.current) datasetInput.current.value = '';
      setSavedJob(job);
      setResult(`Training request saved (${job.id}).`);
    } catch (failure) {
      setError(failure.message);
    } finally {
      inFlight.current = false;
      setUploading(false);
      setPending(false);
    }
  }

  return <>
    <div className="file-preview-header">
      <h3 title={path}>{path || 'File contents'}</h3>
      <form id="training-request" className="training-form" onSubmit={submit} aria-label="Submit training request">
        <label htmlFor="training-epochs">Epochs</label>
        <input id="training-epochs" type="number" min="1" max="2147483647" step="1" required value={epochs} disabled={!runnable || pending} onChange={(event) => setEpochs(event.target.value)} />
        <button type="submit" disabled={!runnable || pending} title={runnable ? 'Save training request' : 'Select a Python (.py) file to train'}>{uploading ? 'Uploading…' : pending ? 'Submitting…' : 'Submit'}</button>
      </form>
    </div>
    <div className="dataset-upload">
      <label htmlFor="training-dataset">Dataset (optional)</label>
      <input ref={datasetInput} id="training-dataset" form="training-request" type="file" disabled={pending} aria-describedby="dataset-help" onChange={(event) => {
        setDatasetFile(event.target.files[0] || null);
        uploadedDataset.current = null;
      }} />
      <p id="dataset-help">Upload a file or a ZIP of your dataset folders. Data is deleted 24 hours after training finishes, fails, or is cancelled.</p>
    </div>
    {result && <p className="tree-message" role="status">{result}</p>}
    {savedJob && <div className="tree-message" aria-live="polite">
      <p>Job status: <strong>{savedJob.status}</strong> · {savedJob.entrypoint}</p>
      {savedJob.dataset && <p>{savedJob.dataset.name}: {savedJob.dataset.deleted_at
        ? 'Dataset expired — upload it again for a new job.'
        : savedJob.dataset.expires_at
          ? `Scheduled for deletion after ${new Date(savedJob.dataset.expires_at).toLocaleString()}.`
          : 'Available for training.'}</p>}
      <p>
      {savedJob.dockerfile_source === 'generated' ? 'Dockerfile generated. ' : 'Project Dockerfile preserved. '}
      <a href={`/api/projects/${projectId}/training-jobs/${savedJob.id}/dockerfile`} download="Dockerfile">Download Dockerfile</a>
      </p>
    </div>}
    {error && <p className="tree-message" role="alert">{error}</p>}
  </>;
}
