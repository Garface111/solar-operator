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

  it("renders Reports tab", () => {
    renderWithRouter();
    expect(screen.getByTestId("bottom-tab-reports")).toBeTruthy();
  });

  it("renders Account tab", () => {
    renderWithRouter();
    expect(screen.getByTestId("bottom-tab-account")).toBeTruthy();
  });

  it("all three tab labels are visible", () => {
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

  it("applies safe-area-inset-bottom padding via inline style attribute", () => {
    renderWithRouter();
    const nav = screen.getByTestId("bottom-tab-bar");
    // jsdom strips env() from computed style; check the raw attribute instead.
    expect(nav.getAttribute("style")).toContain("safe-area-inset-bottom");
  });

  it("nav container has sm:hidden class so it hides on desktop", () => {
    renderWithRouter();
    const nav = screen.getByTestId("bottom-tab-bar");
    expect(nav.className).toContain("sm:hidden");
  });
});
