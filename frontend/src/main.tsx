import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App, makeQueryClient } from "./App";
import { oidcAuth } from "./auth/oidcAuth";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App auth={oidcAuth} client={makeQueryClient()} />
    </BrowserRouter>
  </StrictMode>,
);
