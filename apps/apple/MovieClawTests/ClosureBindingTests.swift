import SwiftUI
import Testing
@testable import MovieClaw

/// `Binding(mcGet:set:)`（Binding(get:set:) 的编译器缺陷替身，见 Core/ClosureBinding.swift）读写都要落到闭包上
@MainActor
struct ClosureBindingTests {
    @Test func forwardsReadsAndWritesToClosures() {
        var stored = "初始"
        var writes = 0
        let binding = Binding<String>(mcGet: { stored }, set: { stored = $0; writes += 1 })
        #expect(binding.wrappedValue == "初始")

        binding.wrappedValue = "改过"
        #expect(stored == "改过")
        #expect(writes == 1)

        // 读取总是现取闭包，外部状态变了 Binding 跟着变
        stored = "外部改"
        #expect(binding.wrappedValue == "外部改")
    }
}
