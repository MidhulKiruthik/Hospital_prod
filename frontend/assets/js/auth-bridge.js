import { a as api, b as http } from './index.js';

const originalLogin = api.login?.bind(api);
if (originalLogin) {
  api.login = async (...args) => {
    const response = await originalLogin(...args);
    const refreshToken = response?.data?.refresh_token;
    if (refreshToken) {
      localStorage.setItem('refresh_token', refreshToken);
    }
    return response;
  };
}

const originalRegister = api.register?.bind(api);
if (originalRegister) {
  api.register = async (...args) => originalRegister(...args);
}

async function handleUnauthorizedResponse(error) {
  const request = error.config || {};
  const isUnauthorized = error.response && error.response.status === 401;

  if (isUnauthorized && !request._retry && request.url !== '/auth/refresh') {
    const refreshToken = localStorage.getItem('refresh_token');
    if (refreshToken) {
      try {
        request._retry = true;
        const refreshResponse = await http.post('/auth/refresh', {
          refresh_token: refreshToken,
        });
        const accessToken = refreshResponse?.data?.access_token || refreshResponse?.data?.token;
        if (accessToken) {
          localStorage.setItem('token', accessToken);
          request.headers = request.headers || {};
          request.headers.Authorization = `Bearer ${accessToken}`;
          return http.request(request);
        }
      } catch {
        // Fall through to forced sign-out below.
      }
    }

    localStorage.removeItem('token');
    localStorage.removeItem('refresh_token');
    localStorage.removeItem('user');
    if (window.location.pathname !== '/login' && window.location.pathname !== '/register') {
      window.location.assign('/login');
    }
  }

  return Promise.reject(error);
}

const responseHandlers = http.interceptors.response.handlers || [];
let patchedResponseHandler = false;
for (const handler of responseHandlers) {
  if (handler && typeof handler.rejected === 'function') {
    handler.rejected = handleUnauthorizedResponse;
    patchedResponseHandler = true;
  }
}

if (!patchedResponseHandler) {
  http.interceptors.response.use((response) => response, handleUnauthorizedResponse);
}

async function performLogout() {
  const refreshToken = localStorage.getItem('refresh_token');
  try {
    await http.post('/auth/logout', refreshToken ? { refresh_token: refreshToken } : {});
  } catch {
    // Ignore network errors during sign-out and clear local state anyway.
  }

  localStorage.removeItem('token');
  localStorage.removeItem('refresh_token');
  localStorage.removeItem('user');
  window.location.assign('/login');
}

api.logout = async () => {
  await performLogout();
};

api.logoutAll = async () => {
  const refreshToken = localStorage.getItem('refresh_token');
  try {
    await http.post('/auth/logout-all', refreshToken ? { refresh_token: refreshToken } : {});
  } catch {
    // Ignore errors and continue with local cleanup.
  }
  localStorage.removeItem('token');
  localStorage.removeItem('refresh_token');
  localStorage.removeItem('user');
  window.location.assign('/login');
};

const backendApis = {
  public: {
    departments: () => http.get('/public/departments'),
    specializations: (params) => http.get('/public/specializations', { params }),
    doctors: (params) => http.get('/public/doctors', { params }),
  },
  admin: {
    dashboard: () => http.get('/admin/dashboard'),
    departments: {
      list: () => http.get('/admin/departments'),
      getById: (departmentId) => http.get(`/admin/departments/${departmentId}`),
      create: (payload) => http.post('/admin/departments', payload),
      update: (departmentId, payload) => http.put(`/admin/departments/${departmentId}`, payload),
      remove: (departmentId) => http.delete(`/admin/departments/${departmentId}`),
    },
    doctors: {
      list: () => http.get('/admin/doctors', { params: { _t: Date.now() } }),
      getById: (doctorId) => http.get(`/admin/doctors/${doctorId}`, { params: { _t: Date.now() } }),
      create: (payload) => http.post('/admin/doctors', payload),
      update: (doctorId, payload) => http.put(`/admin/doctors/${doctorId}`, payload),
      remove: (doctorId) => http.delete(`/admin/doctors/${doctorId}`),
      blacklist: (doctorId) => http.post(`/admin/doctors/${doctorId}/blacklist`),
    },
    patients: {
      list: () => http.get('/admin/patients', { params: { _t: Date.now() } }),
      getById: (patientId) => http.get(`/admin/patients/${patientId}`, { params: { _t: Date.now() } }),
      create: (payload) => http.post('/admin/patients', payload),
      update: (patientId, payload) => http.put(`/admin/patients/${patientId}`, payload),
      remove: (patientId) => http.delete(`/admin/patients/${patientId}`),
    },
    appointments: {
      list: () => http.get('/admin/appointments', { params: { _t: Date.now() } }),
    },
    search: (query) => http.get('/admin/search', { params: { q: query, _t: Date.now() } }),
    analytics: {
      appointmentsTrend: () => http.get('/admin/analytics/appointments-trend', { params: { _t: Date.now() } }),
      specializationDemand: () => http.get('/admin/analytics/specialization-demand', { params: { _t: Date.now() } }),
      monthlyStats: () => http.get('/admin/analytics/monthly-stats', { params: { _t: Date.now() } }),
    },
    reports: {
      generateMonthly: () => http.post('/admin/reports/generate'),
      status: (taskId) => http.get(`/admin/reports/status/${taskId}`, { params: { _t: Date.now() } }),
      downloadDoctors: () => http.get('/admin/reports/download/doctors', { responseType: 'blob' }),
      downloadPatients: () => http.get('/admin/reports/download/patients', { responseType: 'blob' }),
      downloadAppointments: () => http.get('/admin/reports/download/appointments', { responseType: 'blob' }),
      downloadAll: () => http.get('/admin/reports/download/all', { responseType: 'blob' }),
    },
  },
  doctor: {
    dashboard: () => http.get('/doctor/dashboard', { params: { _t: Date.now() } }),
    appointments: {
      list: (fromDate, toDate) => {
        const params = { _t: Date.now() };
        if (fromDate && toDate) {
          params.from_date = fromDate;
          params.to_date = toDate;
        }
        return http.get('/doctor/appointments', { params });
      },
      complete: (appointmentId) => http.post(`/doctor/appointments/${appointmentId}/complete?_t=${Date.now()}`),
      cancel: (appointmentId) => http.post(`/doctor/appointments/${appointmentId}/cancel?_t=${Date.now()}`),
      getDiagnosis: (appointmentId) => http.get(`/doctor/appointments/${appointmentId}/diagnosis?_t=${Date.now()}`),
      saveDiagnosis: (appointmentId, payload) => http.post(`/doctor/appointments/${appointmentId}/diagnosis`, payload),
    },
    patients: {
      list: () => http.get('/doctor/patients', { params: { _t: Date.now() } }),
      history: (patientId) => http.get(`/doctor/patients/${patientId}/history`, { params: { _t: Date.now() } }),
      download: (patientId) => http.get(`/doctor/patients/${patientId}/download`, { responseType: 'blob' }),
    },
    availability: {
      get: (fromDate, toDate) => {
        const params = { _t: Date.now() };
        if (fromDate && toDate) {
          params.from_date = fromDate;
          params.to_date = toDate;
        }
        return http.get('/doctor/availability', { params });
      },
      save: (payload) => http.post('/doctor/availability', payload),
    },
    profile: {
      get: () => http.get('/doctor/profile', { params: { _t: Date.now() } }),
      update: (payload) => http.put('/doctor/profile', payload),
    },
    reports: {
      monthly: () => http.get('/doctor/reports/monthly', { params: { _t: Date.now() } }),
      downloadMonthly: (filename) => http.get(`/doctor/reports/monthly/${encodeURIComponent(filename)}`, { responseType: 'blob' }),
    },
  },
  patient: {
    dashboard: () => http.get('/patient/dashboard', { params: { _t: Date.now() } }),
    doctors: {
      list: (params) => http.get('/patient/doctors', { params }),
      getSlots: (doctorId, date) => http.get(`/patient/doctors/${doctorId}/slots`, { params: { date } }),
    },
    appointments: {
      list: () => http.get('/patient/appointments', { params: { _t: Date.now() } }),
      detail: (appointmentId) => http.get(`/patient/appointments/${appointmentId}`, { params: { _t: Date.now() } }),
      cancel: (appointmentId) => http.post(`/patient/appointments/${appointmentId}/cancel`, null, { params: { _t: Date.now() } }),
      createPaymentOrder: (payload) => http.post('/patient/appointments/create-payment-order', payload),
      verifyPayment: (payload) => http.post('/patient/appointments/verify-payment', payload),
      reschedule: (payload) => http.post('/patient/appointments/reschedule', payload),
    },
    profile: {
      get: () => http.get('/patient/profile', { params: { _t: Date.now() } }),
      update: (payload) => http.put('/patient/profile', payload),
    },
    export: {
      treatmentHistory: () => http.post('/patient/export/treatment-history'),
      status: (taskId) => http.get(`/patient/export/status/${taskId}`, { params: { _t: Date.now() } }),
      download: (filename) => http.get(`/patient/export/download/${filename}`, { responseType: 'blob' }),
    },
    data: {
      download: () => http.get('/patient/data/download', { responseType: 'blob' }),
    },
  },
  departments: {
    listPublic: () => http.get('/public/departments'),
    listAdmin: () => http.get('/admin/departments'),
    getById: (departmentId) => http.get(`/admin/departments/${departmentId}`),
    create: (payload) => http.post('/admin/departments', payload),
    update: (departmentId, payload) => http.put(`/admin/departments/${departmentId}`, payload),
    remove: (departmentId) => http.delete(`/admin/departments/${departmentId}`),
  },
  summaries: {
    get: (summaryId) => http.get(`/summaries/${summaryId}`),
    regenerate: (summaryId) => http.post(`/summaries/${summaryId}/regenerate`),
    review: (summaryId, payload) => http.put(`/summaries/${summaryId}/review`, payload),
    revisions: (summaryId) => http.get(`/summaries/${summaryId}/revisions`),
  },
  forecast: {
    workload: (params) => http.get('/forecast/workload', { params }),
    demand: (params) => http.get('/forecast/demand', { params }),
    history: () => http.get('/forecast/history'),
    bestDoctor: (params) => http.get('/forecast/best-doctor', { params }),
  },
  operations: {
    health: () => http.get('/health'),
    healthDeep: () => http.get('/health/deep'),
    dashboard: () => http.get('/dashboard'),
    adminDashboard: () => http.get('/admin/dashboard'),
    workerStatus: () => http.get('/ops/worker-status'),
    taskEvents: (params) => http.get('/ops/tasks/events', { params }),
    metrics: () => http.get('/metrics/workload-history'),
  },
  audit: {
    logs: (params) => http.get('/audit', { params }),
    integrity: () => http.get('/audit/integrity'),
    securityEvents: (params) => http.get('/security/events', { params }),
  },
  notifications: {
    list: () => http.get('/notifications'),
    markRead: (notificationId) => http.put(`/notifications/${notificationId}/read`),
  },
};

Object.assign(api, {
  backendApis,
  logout: api.logout,
  logoutAll: api.logoutAll,
});

import('./components/AppLayout.js')
  .then((module) => {
    const layout = module?.default;
    if (layout?.methods?.logout) {
      layout.methods.logout = performLogout;
    }
  })
  .catch(() => {
    // Fallback to the document-level logout click handler below.
  });

document.addEventListener(
  'click',
  (event) => {
    const target = event.target instanceof Element ? event.target.closest('.logout-btn') : null;
    if (!target) {
      return;
    }

    event.preventDefault();
    event.stopImmediatePropagation();
    performLogout();
  },
  true,
);

window.hospitalAuthBridge = {
  performLogout,
  backendApis,
};

window.hospitalApi = {
  auth: api,
  http,
  public: backendApis.public,
  admin: backendApis.admin,
  doctor: backendApis.doctor,
  patient: backendApis.patient,
  ...backendApis,
  logout: performLogout,
  logoutAll: api.logoutAll,
};
