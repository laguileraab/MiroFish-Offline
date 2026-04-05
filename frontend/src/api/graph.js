import service, { requestWithRetry, ONTOLOGY_REQUEST_TIMEOUT_MS } from './index'

/**
 * Generate ontology (upload documents and simulation requirements)
 * @param {Object} data - Contains files, simulation_requirement, project_name, etc.
 * @returns {Promise}
 */
export function generateOntology(formData) {
  // No retries: a timeout would otherwise re-submit and start duplicate long LLM jobs
  return service({
    url: '/api/graph/ontology/generate',
    method: 'post',
    data: formData,
    timeout: ONTOLOGY_REQUEST_TIMEOUT_MS,
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  })
}

/**
 * Build graph
 * @param {Object} data - Contains project_id, graph_name, etc.
 * @returns {Promise}
 */
export function buildGraph(data) {
  return requestWithRetry(() =>
    service({
      url: '/api/graph/build',
      method: 'post',
      data
    })
  )
}

/**
 * Query task status
 * @param {String} taskId - Task ID
 * @returns {Promise}
 */
export function getTaskStatus(taskId) {
  return service({
    url: `/api/graph/task/${taskId}`,
    method: 'get'
  })
}

/**
 * Get graph data
 * @param {String} graphId - Graph ID
 * @returns {Promise}
 */
export function getGraphData(graphId) {
  return service({
    url: `/api/graph/data/${graphId}`,
    method: 'get'
  })
}

/**
 * Get project information
 * @param {String} projectId - Project ID
 * @returns {Promise}
 */
export function getProject(projectId) {
  return service({
    url: `/api/graph/project/${projectId}`,
    method: 'get'
  })
}

/**
 * List projects (paginated). Response includes total, offset, limit, count, data.
 * @param {Object} params - { limit?, offset? }
 */
export function listProjects(params = {}) {
  return service.get('/api/graph/project/list', { params })
}
