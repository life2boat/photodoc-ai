const rawApiUrl = import.meta.env.VITE_API_URL || '/api';

export const API_BASE_URL = rawApiUrl.replace(/\/+$/, '');
