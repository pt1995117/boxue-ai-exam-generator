import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import AdminLayout from './layouts/AdminLayout';
import DashboardPage from './pages/DashboardPage';
import MaterialUploadPage from './pages/MaterialUploadPage';
import SliceReviewPage from './pages/SliceReviewPage';
import MappingReviewPage from './pages/MappingReviewPage';
import AIGeneratePage from './pages/AIGeneratePage';
import AIGenerateTaskDetailPage from './pages/AIGenerateTaskDetailPage';
import QuestionBankPage from './pages/QuestionBankPage';
import CityAdminPage from './pages/CityAdminPage';
import GlobalKeyConfigPage from './pages/GlobalKeyConfigPage';
import QualityEvaluationPage from './pages/QualityEvaluationPage';
import VersionManagementPage from './pages/VersionManagementPage';
import JudgeTaskPage from './pages/JudgeTaskPage';
import JudgeTaskDetailPage from './pages/JudgeTaskDetailPage';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AdminLayout />}>
        <Route index element={<DashboardPage />} />
        <Route path="materials" element={<MaterialUploadPage />} />
        <Route path="slice-review" element={<SliceReviewPage />} />
        <Route path="mapping-review" element={<MappingReviewPage />} />
        <Route path="ai-generate" element={<AIGeneratePage />} />
        <Route path="ai-generate/tasks/:taskId" element={<AIGenerateTaskDetailPage />} />
        <Route path="qa-evaluation" element={<QualityEvaluationPage />} />
        <Route path="judge-tasks" element={<JudgeTaskPage />} />
        <Route path="judge-tasks/:taskId" element={<JudgeTaskDetailPage />} />
        <Route path="version-management" element={<VersionManagementPage />} />
        <Route path="question-bank" element={<QuestionBankPage />} />
        <Route path="city-admin" element={<CityAdminPage />} />
        <Route path="global-key-config" element={<GlobalKeyConfigPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
