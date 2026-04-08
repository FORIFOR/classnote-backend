const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchApi(path: string, options: RequestInit = {}) {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const rawBody = await response.text().catch(() => "");
      let errorData: any = {};
      try { errorData = JSON.parse(rawBody); } catch {}
      throw new Error(errorData.detail || `API error: ${response.status}`);
    }

    return response.json();
  } catch (error: any) {
    console.error("API Error:", error);
    throw error;
  }
}
