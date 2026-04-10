import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
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

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AdminLayout />}>
        <Route index element={<PageErrorBoundary><DashboardPage /></PageErrorBoundary>} />
        <Route path="materials" element={<PageErrorBoundary><MaterialUploadPage /></PageErrorBoundary>} />
        <Route path="slice-review" element={<PageErrorBoundary><SliceReviewPage /></PageErrorBoundary>} />
        <Route path="mapping-review" element={<PageErrorBoundary><MappingReviewPage /></PageErrorBoundary>} />
        <Route path="ai-generate" element={<PageErrorBoundary><AIGeneratePage /></PageErrorBoundary>} />
        <Route path="generate-templates" element={<PageErrorBoundary><GenerateTemplatePage /></PageErrorBoundary>} />
        <Route path="ai-generate/tasks/:taskId" element={<PageErrorBoundary><AIGenerateTaskDetailPage /></PageErrorBoundary>} />
        <Route path="qa-evaluation" element={<PageErrorBoundary><QualityEvaluationPage /></PageErrorBoundary>} />
        <Route path="judge-tasks" element={<PageErrorBoundary><JudgeTaskPage /></PageErrorBoundary>} />
        <Route path="judge-tasks/:taskId" element={<PageErrorBoundary><JudgeTaskDetailPage /></PageErrorBoundary>} />
        <Route path="version-management" element={<PageErrorBoundary><VersionManagementPage /></PageErrorBoundary>} />
        <Route path="question-bank" element={<PageErrorBoundary><QuestionBankPage /></PageErrorBoundary>} />
        <Route path="city-admin" element={<PageErrorBoundary><CityAdminPage /></PageErrorBoundary>} />
        <Route path="global-key-config" element={<PageErrorBoundary><GlobalKeyConfigPage /></PageErrorBoundary>} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
