export function pendingApprovals(approvals) {
  return approvals.filter((approval) => approval.state === "pending");
}
