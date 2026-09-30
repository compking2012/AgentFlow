import type { WorkflowProject, WorkflowVersion } from './types';
export type ProjectSelection = { projectId?: string; versionId?: string; runId?: string };

/** A direct run link owns selection until its project index has caught up. */
export function resolveProjectSelection(projects: WorkflowProject[], selection: ProjectSelection): {
  project?: WorkflowProject; version?: WorkflowVersion; runId: string;
} {
  if (selection.runId) {
    const project = projects.find(item => item.versions.some(version => version.run_id === selection.runId));
    return { project, version: project?.versions.find(version => version.run_id === selection.runId), runId: selection.runId };
  }
  const project = selection.projectId ? projects.find(item => item.id === selection.projectId) : projects.find(item => !item.deleted);
  const version = project?.versions.find(item => item.id === (selection.versionId || project.default_version_id));
  return { project, version, runId: version?.run_id || '' };
}
