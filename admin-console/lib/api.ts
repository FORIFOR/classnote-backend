import { auth } from "./firebase";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchApi(path: string, options: RequestInit = {}) {
  const user = auth.currentUser;
  if (!user) {
    throw new Error("User not authenticated");
  }

  // Debug log to verify Env var
  console.log("Fetching API:", `${API_BASE_URL}${path}`);

  const token = await user.getIdToken();

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
    ...options.headers,
  };

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const errorMessage = errorData.detail || `API request failed: ${response.status} ${response.statusText}`;
      console.error("API Error Details:", {
        path,
        status: response.status,
        statusText: response.statusText,
        detail: errorData
      });
      throw new Error(errorMessage);
    }

    return response.json();
  } catch (error: any) {
    console.error("Fetch API Network/Parsing Error:", error);
    throw error;
  }
}
