import { Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { AuditEscalations } from "./pages/AuditEscalations";
import { Dashboard } from "./pages/Dashboard";
import { LeakDetail } from "./pages/LeakDetail";
import { Leaks } from "./pages/Leaks";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="leaks" element={<Leaks />} />
        <Route path="leaks/:policyNo" element={<LeakDetail />} />
        <Route path="audit" element={<AuditEscalations />} />
      </Route>
    </Routes>
  );
}
