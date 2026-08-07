import { AnalyzeView } from "./AnalyzeView";

interface Props {
  params: Promise<{ ticker: string }>;
}

export default async function AnalyzePage({ params }: Props) {
  const { ticker } = await params;
  return <AnalyzeView ticker={decodeURIComponent(ticker).toUpperCase()} />;
}
