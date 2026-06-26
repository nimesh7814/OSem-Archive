const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/$/, '');

export const apiUrl = (path: string, params?: URLSearchParams) => {
  const query = params?.toString();
  return `${API_BASE_URL}${path}${query ? `?${query}` : ''}`;
};
