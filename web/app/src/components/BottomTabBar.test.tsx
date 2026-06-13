import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import React from "react";
import { BottomTabBar } from "./BottomTabBar";

function renderWithRouter(initialPath = "/clients") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <BottomTabBar />
    </MemoryRouter>,
  );
}

describe("BottomTabBar", () => {
  it("renders a navigation landmark with an accessible label", () => {
    renderWithRouter();
    expect(
      screen.getByRole("navigation", { name: "Main navigation" }),
    ).toBeTruthy();
  });

  it("renders exactly three tab links", () => {
    renderWithRouter();
    expect(screen.getAllByRole("link")).toHaveLength(3);
  });

  it("renders Clients tab", () => {
    renderWithRouter();
    expect(screen.getByTestId("bottom-tab-clients")).toBeTruthy();
  });

  it("does not render an Arrays tab (retired)", () => {
    renderWithRouter();
    expect(screen.queryByTestId("bottom-tab-arrays")).toBeNull();
  });

  it("renders Reports tab", () => {
    renderWithRouter();
    expect(screen.getByTestId("bottom-tab-reports")).toBeTruthy();
  });

  it("renders Account tab", () => {
    renderWithRouter();
    expect(screen.getByTestId("bottom-tab-account")).toBeTruthy();
  });

  it("all tab labels are visible", () => {
    renderWithRouter();
    expect(screen.getByText("Clients")).toBeTruthy();
    expect(screen.getByText("Reports")).toBeTruthy();
    expect(screen.getByText("Account")).toBeTruthy();
  });

  it("each tab has an accessible aria-label", () => {
    renderWithRouter();
    expect(screen.getByRole("link", { name: "Clients" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Reports" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Account" })).toBeTruthy();
  });

  it("active tab has emerald (primary-600) text class on /clients route", () => {
    renderWithRouter("/clients");
    const tab = screen.getByTestId("bottom-tab-clients");
    expect(tab.className).toContain("text-primary-600");
  });

  it("inactive tabs do not have active class on /clients route", () => {
    renderWithRouter("/clients");
    expect(
      screen.getByTestId("bottom-tab-reports").className,
    ).not.toContain("text-primary-600");
    expect(
      screen.getByTestId("bottom-tab-account").className,
    ).not.toContain("text-primary-600");
  });

  it("active tab changes when route is /reports", () => {
    renderWithRouter("/reports");
    expect(
      screen.getByTestId("bottom-tab-reports").className,
    ).toContain("text-primary-600");
    expect(
      screen.getByTestId("bottom-tab-clients").className,
    ).not.toContain("text-primary-600");
  });

  it("is fixed-positioned with z-30 so it always floats at the bottom", () => {
    renderWithRouter();
    const nav = screen.getByTestId("bottom-tab-bar");
    // Fixed + z-30 keeps the bar above page content but below the z-40 MindButton.
    expect(nav.className).toContain("fixed");
    expect(nav.className).toContain("z-30");
  });

  it("nav container has sm:hidden class so it hides on desktop", () => {
    renderWithRouter();
    const nav = screen.getByTestId("bottom-tab-bar");
    expect(nav.className).toContain("sm:hidden");
  });
});
