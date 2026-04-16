import React from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import PageErrorBoundary from './components/PageErrorBoundary';
import AdminLayout from './layouts/AdminLayout';
import DashboardPage from './pages/DashboardPage';
import MaterialUploadPage from './pages/MaterialUploadPage';
import SliceReviewPage from './pages/SliceReviewPage';
import MappingReviewPage from './pages/MappingReviewPage';
import AIGeneratePage from './pages/AIGeneratePage';
import AIGenerateTaskDetailPage from './pages/AIGenerateTaskDetailPage';
import GenerateTemplatePage from './pages/GenerateTemplatePage';
import QuestionBankPage from './pages/QuestionBankPage';
import CityAdminPage from './pages/CityAdminPage';
import GlobalKeyConfigPage from './pages/GlobalKeyConfigPage';
import QualityEvaluationPage from './pages/QualityEvaluationPage';
import VersionManagementPage from './pages/VersionManagementPage';
import JudgeTaskPage from './pages/JudgeTaskPage';
import JudgeTaskDetailPage from './pages/JudgeTaskDetailPage';
import QuestionTunePage from './pages/QuestionTunePage';

export default function App() {
  const location = useLocation();
  const resetKey = location.key || location.pathname;
  const withBoundary = (node) => (
    <PageErrorBoundary key={resetKey} resetKey={resetKey}>
      {node}
    </PageErrorBoundary>
  );
  return (
    <Routes>
      <Route path="/" element={<AdminLayout />}>
        <Route index element={withBoundary(<DashboardPage />)} />
        <Route path="materials" element={withBoundary(<MaterialUploadPage />)} />
        <Route path="slice-review" element={withBoundary(<SliceReviewPage />)} />
        <Route path="mapping-review" element={withBoundary(<MappingReviewPage />)} />
        <Route path="ai-generate" element={withBoundary(<AIGeneratePage />)} />
        <Route path="generate-templates" element={withBoundary(<GenerateTemplatePage />)} />
        <Route path="ai-generate/tasks/:taskId" element={withBoundary(<AIGenerateTaskDetailPage />)} />
        <Route path="qa-evaluation" element={withBoundary(<QualityEvaluationPage />)} />
        <Route path="judge-tasks" element={withBoundary(<JudgeTaskPage />)} />
        <Route path="judge-tasks/:taskId" element={withBoundary(<JudgeTaskDetailPage />)} />
        <Route path="version-management" element={withBoundary(<VersionManagementPage />)} />
        <Route path="question-bank" element={withBoundary(<QuestionBankPage />)} />
        <Route path="question-bank/:questionId/tune" element={withBoundary(<QuestionTunePage />)} />
        <Route path="city-admin" element={withBoundary(<CityAdminPage />)} />
        <Route path="global-key-config" element={withBoundary(<GlobalKeyConfigPage />)} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
