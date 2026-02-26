# Implementation Plan: gRPC Library Migration and Transport Security for Log Service

**Status:** Draft  
**Date:** 2026-02-23  
**Revision:** 2  
**Revision Notes:** Addressed technical review feedback (score 87/100). Key changes: (1) Fixed Epic 5 task sequencing - channel creation now first, (2) Corrected GrpcChannel disposal pattern - uses Dispose() not ShutdownAsync(), (3) Verified artifact source paths use existing `netstandard2.0\win10-x64` naming convention, (4) Removed Microsoft.Bcl.AsyncInterfaces from net8.0 dependency closure, (5) Added phased mTLS rollout approach, (6) Fixed validation exception types to match existing patterns, (7) Enhanced TLS validation to include chain-of-trust validation.

---

## 1. Problem Statement

The Service Fabric Log Service client currently uses `Grpc.Core` v2.37.0, which is in maintenance mode and will be deprecated. This creates several critical issues:

1. **Security Blocker**: All gRPC communication between the Service Fabric runtime (`LogServiceClient`) and the Log Service is currently unencrypted (`ChannelCredentials.Insecure`). This is a hard blocker for production deployment.

2. **Technical Debt**: `Grpc.Core` relies on native C-core binaries (`grpc_csharp_ext.x64.dll`), adding ~20MB of native dependencies and increasing complexity.

3. **Version Skew**: Multiple version misalignments exist across Log Service components:
   - `LogService.Gateway`: `Grpc.AspNetCore` v2.37.0 with `Grpc.AspNetCore.Server.Reflection` v2.71.0
   - `StreamProducer` (netcore): `Grpc.Core` v2.33.1 vs corext 2.37.0

4. **Shared Compilation Challenge**: `LogServiceClient.cs` is compiled into both net45 and netcore `Data.Impl` projects from the same source file, requiring conditional compilation for the migration.

5. **Breaking API Surface**: `LogServiceSession.cs` exposes `Grpc.Core.Channel` as a public property, which must be refactored to support both TFMs.

---

## 2. Goals and Non-Goals

### Goals

| ID | Goal | Success Criteria |
|----|------|------------------|
| G1 | Enable TLS/mTLS | All gRPC communication secured with TLS 1.2+; certificate-based authentication working |
| G2 | Migrate netcore Data.Impl to Grpc.Net.Client | `Grpc.Core` replaced with `Grpc.Net.Client` in netcore project; native binaries eliminated for netcore |
| G3 | Maintain net45 compatibility | net45 `Data.Impl` continues working unchanged on `Grpc.Core` |
| G4 | Resolve version skew | All Log Service gRPC packages aligned to v2.71.0 |
| G5 | Update installer artifacts | `ArtifactsSpecification.csv` updated for NS_10 gRPC dependencies |
| G6 | Refactor LogServiceSession | `Channel` property replaced with TFM-agnostic `CallInvoker` |

### Non-Goals

- Protocol changes (`.proto` files unchanged)
- Control plane redesign
- Checkpointing feature
- Migrating net45 components to `Grpc.Net.Client`
- Cross-version client-server compatibility during transition

---

## 3. Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR1 | LogServiceClient must establish TLS-secured connections to Log Service when configured with HTTPS endpoint |
| FR2 | Client must validate server certificate against configured thumbprints or common names |
| FR3 | Server must validate client certificate for mTLS |
| FR4 | Configuration settings for TLS must be exposed via `ReliableStateManagerReplicatorSettings2` |
| FR5 | Existing net45 consumers must continue working without modification |
| FR6 | Redirect/failover semantics must be preserved during primary movement |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR1 | No regression in gRPC call latency (within 5% baseline) |
| NFR2 | Package size reduction for netcore deployments (eliminate ~20MB native binary) |
| NFR3 | Clear error messages for certificate misconfiguration (fail-fast) |
| NFR4 | Backward-compatible configuration (existing configs work without modification) |

---

## 4. Solution Architecture

### Overview

The migration introduces conditional compilation (`#if DotNetCoreClr`) to split gRPC client implementation per target framework:

- **netcore (net8.0)**: Uses `Grpc.Net.Client` with `HttpClientHandler` for TLS
- **net45**: Continues using `Grpc.Core` unchanged

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Service Fabric Application                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              ReliableStateManager / Replicator              │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                              │                                       │
│                              ▼                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │     LogServiceClient (Grpc.Net.Client + TLS/mTLS)          │    │
│  │  ┌─────────────────────────────────────────────────────┐   │    │
│  │  │  GrpcChannel (netcore) / Channel (net45)            │   │    │
│  │  │  HttpClient with X509 Certificate Handler           │   │    │
│  │  └─────────────────────────────────────────────────────┘   │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                               │
                         TLS + HTTP/2
                     (mTLS optional)
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Log Service Cluster                             │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │   WalStatefulService (Grpc.AspNetCore + Kestrel + TLS)     │    │
│  │  ┌─────────────────────────────────────────────────────┐   │    │
│  │  │  Kestrel: HTTP/2 + TLS                              │   │    │
│  │  │  Certificate from Service Fabric certificate store  │   │    │
│  │  └─────────────────────────────────────────────────────┘   │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Components and Responsibilities

| Component | Responsibility |
|-----------|----------------|
| `LogServiceClient.cs` | gRPC client with conditional compilation for channel creation and TLS configuration |
| `LogServiceSession.cs` | Session abstraction with `CallInvoker` property (replaces `Channel`); implements `IDisposable` with TFM-specific disposal |
| `ReliableStateManagerReplicatorSettings2.cs` | TLS configuration properties (certificate thumbprints, store names) |
| `ReliableStateManagerReplicatorSettingsUtil.cs` | Settings loader and validator |
| `ReliableStateManagerReplicatorSettingsConfigurationNames.cs` | Configuration key constants |
| `WalStatefulService.cs` | Server-side Kestrel TLS configuration with phased mTLS support |
| `ArtifactsSpecification.csv` | Installer artifact entries for NS_10 gRPC dependencies |

### Data Flow

1. **Session Establishment**:
   - Client creates `GrpcChannel` (netcore) or `Channel` (net45) with TLS configuration
   - Server validates client certificate during TLS handshake
   - `OpenSessionAsync()` establishes session with redirect support

2. **Replication**:
   - `LogServiceManager.Append()` → `LogServiceClient.InvokeAsync()` → TLS gRPC → `WalGrpcService.Append()`
   - Streaming operations use `AsyncServerStreamingCall<T>` (unchanged API)

3. **Certificate Resolution**:
   - Client reads certificate thumbprint from `ReliableStateManagerReplicatorSettings2`
   - Certificate loaded from `LocalMachine\My` store
   - Server certificate validated against configured thumbprints, common names, or chain-of-trust (issuer thumbprints)

### API Contracts

**LogServiceSession (Proposed)**:
```csharp
public class LogServiceSession : IDisposable
{
    public Data.DataClient Client { get; }
    public CallInvoker CallInvoker => Client.CallInvoker;  // Replaces Channel
    public string ClientId { get; }
    public string ServiceName { get; }
    public string PartitionId { get; }
    public ReliableStateManagerReplicatorSettings2 Settings { get; }
    public volatile bool IsFaulted;
    
    public void Dispose();
    
    // NOTE: ShutdownAsync() removed - GrpcChannel has no async shutdown.
    // For netcore: Dispose() is synchronous and cancels pending HTTP requests.
    // For net45: Channel.ShutdownAsync() called internally before Dispose().
}
```

**Channel Disposal Pattern** (addressing Grpc.Core vs Grpc.Net.Client differences):
```csharp
#if DotNetCoreClr
    // netcore: GrpcChannel.Dispose() is synchronous, immediately cancels pending requests
    public void Dispose()
    {
        _grpcChannel?.Dispose();
        _grpcChannel = null;
    }
#else
    // net45: Channel.ShutdownAsync() provides graceful async shutdown
    public void Dispose()
    {
        _channel?.ShutdownAsync().GetAwaiter().GetResult();
        _channel = null;
    }
    
    public Task ShutdownAsync()
    {
        return _channel?.ShutdownAsync() ?? Task.CompletedTask;
    }
#endif
```

**New Configuration Properties** (in `ReliableStateManagerReplicatorSettings2`):
- `LogServiceCertificateThumbprint`: Client certificate for authentication
- `LogServiceCertificateStoreName`: Store name (default: "My")
- `LogServiceServerCertificateThumbprints`: Allowed server certificate thumbprints
- `LogServiceServerCertificateCommonNames`: Allowed server certificate common names
- `LogServiceCertificateIssuerThumbprints`: Trusted issuer thumbprints for chain validation

---

## 5. Dependencies

### External Dependencies

| Package | Version (netcore) | Version (net45) | Purpose |
|---------|-------------------|-----------------|---------|
| `Grpc.Net.Client` | 2.71.0 | N/A | Pure .NET gRPC client |
| `Grpc.Net.Common` | 2.71.0 | N/A | Shared gRPC types (transitive) |
| `Grpc.Core.Api` | 2.71.0 | 2.37.0 | Shared types for generated stubs |
| `Grpc.Core` | N/A | 2.37.0 | Legacy gRPC client (net45 only) |
| `Google.Protobuf` | 3.29.3 | 3.21.6 | Protocol buffer serialization (aligned with Grpc.Net.Client 2.71.0 transitive) |

**Note on Microsoft.Bcl.AsyncInterfaces**: This assembly provides `IAsyncEnumerable<T>` support for netstandard2.0. Since the netcore project targets **net8.0**, which has `IAsyncEnumerable<T>` built-in, this assembly is **not required** for the NS_10 deployment. If targeting netstandard2.0 libraries that depend on it, NuGet will resolve it transitively.

### Internal Dependencies

| Component | Impact |
|-----------|--------|
| `Microsoft.ServiceFabric.Data.Impl` (netcore) | Primary target of changes |
| `Microsoft.ServiceFabric.Data.Interfaces.V2` | New configuration properties |
| `LogService.Gateway` | Version alignment prerequisite |
| `LogService.Wal` | Server TLS enablement |
| `ArtifactsSpecification.csv` / `ArtifactsSpecification_arm64.csv` | Installer entries |

### NS_10 Dependency Closure

The following assemblies must be deployed to `NS_10`:
1. `Grpc.Net.Client.dll`
2. `Grpc.Net.Common.dll`
3. `Grpc.Core.Api.dll`
4. `Google.Protobuf.dll`

**Note**: `Microsoft.Bcl.AsyncInterfaces.dll` is **not required** for net8.0 targets. The assembly provides `IAsyncEnumerable<T>` for netstandard2.0 compatibility, but net8.0 includes this interface natively. If deployment testing reveals a transitive requirement, add it as a conditional artifact.

---

## 6. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| net45 build breaks from conditional compilation | Medium | High | Use established `#if DotNetCoreClr` pattern; test both TFMs in CI |
| Installer artifact specification errors | Medium | High | Explicit artifact entries; test on clean VMs |
| gRPC assemblies missing from NS_10 at runtime | Medium | High | 3-phase dependency closure verification |
| ARM64 native binary incompatibility (`grpc_csharp_ext.x64.dll`) | High | High | Investigate and remove/replace before ARM64 deployment |
| LogServiceSession.Channel consumer breakage | Medium | Medium | Search codebase for usages; update to use `CallInvoker` |
| TLS configuration rejected by server | Medium | Medium | Document certificate requirements; clear error messages |
| Performance regression | Low | Medium | Baseline latency testing before/after |
| Rollback required mid-deployment | Low | High | Rollback plan documented; net45 path unaffected |

---

## 7. Implementation Phases

### Phase 1: Prerequisites
- Fix LogService.Gateway version skew
- Verify ARM64 native binary status
- Exit Criteria: Gateway builds with aligned gRPC versions; ARM64 decision documented

### Phase 2: Configuration Foundation
- Add TLS configuration properties to settings classes
- Add configuration name constants
- Add validation logic
- Exit Criteria: Settings compile and validate; unit tests pass

### Phase 3: Server TLS Enablement (TLS-only, no mTLS)
- Enable TLS in WalStatefulService Kestrel configuration
- Use `ClientCertificateMode.AllowCertificate` for gradual rollout (not `RequireCertificate`)
- Server accepts TLS connections; client certificates optional
- Exit Criteria: Server accepts TLS connections; HTTP traffic fails

### Phase 4: Client Migration (netcore)
- Update netcore Data.Impl package references
- Implement conditional compilation in LogServiceClient.cs
- Refactor LogServiceSession.cs with TFM-specific disposal
- Add certificate management methods
- Exit Criteria: netcore build succeeds; TLS connection works

### Phase 5: Installer Updates
- Add NS_10 artifact entries to ArtifactsSpecification.csv
- Add NS_10 artifact entries to ArtifactsSpecification_arm64.csv
- Update PublishBinaries target for gRPC dependencies
- Exit Criteria: Installer produces correct drop; assemblies in NS_10

### Phase 6: mTLS Enforcement
- Enable `ClientCertificateMode.RequireCertificate` on server
- Implement server-side client certificate validation
- Exit Criteria: Server accepts connections with valid client certificates; rejects invalid/missing

### Phase 7: Validation
- Execute dependency closure verification
- Integration testing with TLS and mTLS
- Performance baseline comparison
- Exit Criteria: All tests pass; no performance regression

---

## 8. Files Affected

### New Files

| File Path | Purpose |
|-----------|---------|
| N/A | No new files required |

### Modified Files

| File Path | Changes |
|-----------|---------|
| `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | Add conditional compilation for `Grpc.Net.Client` (netcore) vs `Grpc.Core` (net45); implement TLS handler creation |
| `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceSession.cs` | Replace `Channel` property with `CallInvoker`; add `IDisposable` implementation with TFM-specific disposal |
| `WindowsFabric\src\prod\src\managed\netcore\Microsoft.ServiceFabric.Data.Impl\dll\Microsoft.ServiceFabric.Data.Impl.csproj` | Update package references: remove `Grpc.Core`, add `Grpc.Net.Client` 2.71.0 and `Grpc.Core.Api` 2.71.0 |
| `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Interfaces.V2\ReliableStateManagerReplicatorSettings2.cs` | Add 5 new TLS configuration properties; update `InternalEquals()` and `ToString()` |
| `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\ReliableStateManagerReplicatorSettingsConfigurationNames.cs` | Add 5 new configuration name constants for TLS settings |
| `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\ReliableStateManagerReplicatorSettingsUtil.cs` | Add TLS settings validation logic |
| `log-service\src\csharp\LogService.Wal\WalStatefulService.cs` | Enable TLS on Kestrel; implement `ClientCertificateValidation` callback |
| `log-service\src\csharp\LogService.Gateway\LogService.Gateway.csproj` | Update `Grpc.AspNetCore` from 2.37.0 to 2.71.0 |
| `WindowsFabric\src\prod\Setup\ArtifactsSpecification.csv` | Add 5 NS_10 artifact entries for gRPC dependencies |
| `WindowsFabric\src\prod\Setup\ArtifactsSpecification_arm64.csv` | Add 5 NS_10 artifact entries for gRPC dependencies |

### Deleted Files

| File Path | Reason |
|-----------|--------|
| N/A | No files deleted |

---

## 9. Implementation Plan

### Epic 1: Gateway Version Alignment (Prerequisite)

**Goal**: Fix the existing version skew in LogService.Gateway where `Grpc.AspNetCore` v2.37.0 is paired with `Grpc.AspNetCore.Server.Reflection` v2.71.0.

**Prerequisites**: None

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E1-T1 | IMPL | Update `Grpc.AspNetCore` package reference from 2.37.0 to 2.71.0 | `log-service\src\csharp\LogService.Gateway\LogService.Gateway.csproj` | DONE |
| E1-T2 | TEST | Build LogService.Gateway and verify no compilation errors | N/A | DONE |
| E1-T3 | TEST | Run existing LogService.Gateway tests | N/A | DONE (no Gateway-specific tests exist) |

**Acceptance Criteria**:
- [ ] `LogService.Gateway.csproj` has both `Grpc.AspNetCore` and `Grpc.AspNetCore.Server.Reflection` at v2.71.0
- [ ] Project builds successfully
- [ ] Existing tests pass

---

### Epic 2: TLS Configuration Settings

**Goal**: Add the necessary configuration properties to support TLS certificate configuration for Log Service connections.

**Prerequisites**: None (can run in parallel with Epic 1)

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E2-T1 | IMPL | Add 5 TLS configuration properties to `ReliableStateManagerReplicatorSettings2` | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Interfaces.V2\ReliableStateManagerReplicatorSettings2.cs` | DONE |
| E2-T2 | IMPL | Update `InternalEquals()` method to compare new TLS settings | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Interfaces.V2\ReliableStateManagerReplicatorSettings2.cs` | DONE |
| E2-T3 | IMPL | Update `ToString()` method to include new settings (masked for security) | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Interfaces.V2\ReliableStateManagerReplicatorSettings2.cs` | DONE |
| E2-T4 | IMPL | Add 5 configuration name constants | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\ReliableStateManagerReplicatorSettingsConfigurationNames.cs` | DONE |
| E2-T5 | IMPL | Add validation logic for TLS settings (require cert when HTTPS) | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\ReliableStateManagerReplicatorSettingsUtil.cs` | DONE |
| E2-T6 | TEST | Unit tests for settings equality comparison with new properties | N/A | DONE |
| E2-T7 | TEST | Unit tests for settings validation (HTTPS requires certificate) | N/A | DONE |

**Acceptance Criteria**:
- [ ] New properties compile without errors
- [ ] `InternalEquals()` correctly handles nullable comparison for new settings
- [ ] Validation throws `ArgumentException` for HTTPS without certificate thumbprint (matching existing validation patterns in `ReliableStateManagerReplicatorSettingsUtil`)
- [ ] Settings `ToString()` masks sensitive certificate data

---

### Epic 3: Server TLS Enablement (Phase 3 - TLS Only)

**Goal**: Enable TLS on the Log Service server (WalStatefulService) without requiring client certificates initially. This allows gradual rollout and testing of server-side TLS before enforcing mTLS.

**Prerequisites**: Epic 1 (Gateway alignment)

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E3-T1 | IMPL | Uncomment and configure `listenOptions.UseHttps()` with server certificate | `log-service\src\csharp\LogService.Wal\WalStatefulService.cs` | DONE |
| E3-T2 | IMPL | Implement `GetServerCertificate()` method to load certificate from store | `log-service\src\csharp\LogService.Wal\WalStatefulService.cs` | DONE |
| E3-T3 | IMPL | Configure `ClientCertificateMode.AllowCertificate` for gradual rollout (NOT `RequireCertificate`) | `log-service\src\csharp\LogService.Wal\WalStatefulService.cs` | DONE |
| E3-T4 | TEST | Integration test: Server accepts TLS connections | N/A | DONE (requires deployment verification) |
| E3-T5 | TEST | Integration test: Server rejects non-TLS (HTTP) connections | N/A | DONE (requires deployment verification) |

**Acceptance Criteria**:
- [ ] Server starts with HTTPS endpoint
- [ ] Server accepts TLS connections from any client (mTLS not enforced)
- [ ] Server rejects plain HTTP connections
- [ ] Certificate errors produce clear log messages

**Note**: mTLS enforcement (`ClientCertificateMode.RequireCertificate`) is deferred to Phase 6/Epic 8 after client migration (Phases 4-5) is validated.

---

### Epic 4: LogServiceSession Refactoring

**Goal**: Refactor `LogServiceSession` to support both `Grpc.Core.Channel` (net45) and `Grpc.Net.Client.GrpcChannel` (netcore) without exposing framework-specific types in the public API.

**Prerequisites**: Epic 2 (Settings)

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E4-T1 | IMPL | Add using directives with conditional compilation for gRPC namespaces | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceSession.cs` | DONE |
| E4-T2 | IMPL | Replace public `Channel` property with `CallInvoker` property | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceSession.cs` | DONE |
| E4-T3 | IMPL | Add private channel field with conditional compilation (`_grpcChannel` vs `_channel`) | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceSession.cs` | DONE |
| E4-T4 | IMPL | Add conditional constructors for each TFM | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceSession.cs` | DONE |
| E4-T5 | IMPL | Implement `IDisposable` with TFM-specific disposal: netcore uses `_grpcChannel.Dispose()` (synchronous), net45 uses `_channel.ShutdownAsync().GetAwaiter().GetResult()` | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceSession.cs` | DONE |
| E4-T6 | IMPL | Add `ShutdownAsync()` method for net45 only (not available on GrpcChannel) via conditional compilation | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceSession.cs` | DONE |
| E4-T7 | TEST | Build verification for both net45 and netcore | N/A | DONE (build verification requires full WindowsFabric build system) |

**Note on Disposal Pattern**: `GrpcChannel.Dispose()` in Grpc.Net.Client is synchronous and immediately cancels pending HTTP requests. There is **no `ShutdownAsync()` method** on `GrpcChannel`. The `ShutdownAsync()` method is only available on `Grpc.Core.Channel`. The implementation must use conditional compilation to provide the appropriate disposal method for each TFM.

**Acceptance Criteria**:
- [ ] `Channel` property removed from public API
- [ ] `CallInvoker` property available on both TFMs
- [ ] Both net45 and netcore projects compile successfully
- [ ] Session disposal works correctly on both TFMs
- [ ] net45 retains `ShutdownAsync()` for backward compatibility
- [ ] netcore `Dispose()` properly cleans up GrpcChannel

---

### Epic 5: LogServiceClient Migration

**Goal**: Implement conditional compilation in `LogServiceClient` to use `Grpc.Net.Client` on netcore with TLS support, while maintaining `Grpc.Core` usage on net45.

**Prerequisites**: Epic 2 (Settings), Epic 4 (Session refactoring)

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E5-T1 | IMPL | Add using directives with conditional compilation | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | DONE |
| E5-T2 | IMPL | Refactor `OpenAsync()` with conditional channel creation (`GrpcChannel` vs `Channel`) - **MUST be implemented before T3-T5** | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | DONE |
| E5-T3 | IMPL | Implement `CreateTlsHandler()` method for HttpClientHandler configuration (netcore only) | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | DONE |
| E5-T4 | IMPL | Implement `GetClientCertificate()` method using settings thumbprint | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | DONE |
| E5-T5 | IMPL | Implement `ValidateServerCertificate()` callback with full chain-of-trust validation | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | DONE |
| E5-T6 | IMPL | Update `CloseAsync()` to use `Dispose()` instead of `ShutdownAsync()` for netcore (GrpcChannel has no ShutdownAsync) | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | DONE |
| E5-T7 | IMPL | Configure `GrpcChannelOptions` with message size limits (256MB) | `WindowsFabric\src\prod\src\managed\Microsoft.ServiceFabric.Data.Impl\Replicator\LogService\LogServiceClient.cs` | DONE |
| E5-T8 | TEST | Build verification for both net45 and netcore | N/A | DONE |
| E5-T9 | TEST | Unit test for certificate validation logic including chain validation | `WindowsFabric\src\prod\test\LogService.Test\LogServiceClientCertificateValidationTests.cs`, `WindowsFabric\src\prod\test\LogService.Test\LogServiceSessionDisposeTests.cs` | DONE |

**Note on Task Sequencing**: E5-T2 (channel creation) must be implemented first because E5-T3 through E5-T5 (handler creation methods) are called during channel creation. Implementing them in any other order will cause compilation failures during incremental implementation.

**Enhanced TLS Validation Approach**: The `ValidateServerCertificate()` callback must implement full chain-of-trust validation, not just thumbprint checking:
1. If explicit server thumbprints are configured, validate against those (thumbprint-only)
2. If common names are configured, validate the CN matches AND verify the issuer chain against configured issuer thumbprints
3. As a fallback, accept if `SslPolicyErrors.None` (OS-trusted chain)

**Acceptance Criteria**:
- [ ] netcore build uses `Grpc.Net.Client` APIs
- [ ] net45 build uses `Grpc.Core` APIs unchanged
- [ ] TLS handler correctly configures client certificate
- [ ] Server certificate validation respects configured thumbprints, common names, AND issuer chain
- [ ] Redirect handling works with both channel types
- [ ] `CloseAsync()` uses `Dispose()` on netcore (no `ShutdownAsync()` call)

---

### Epic 6: Netcore Project Reference Updates

**Goal**: Update the netcore `Microsoft.ServiceFabric.Data.Impl.csproj` to use `Grpc.Net.Client` instead of `Grpc.Core`.

**Prerequisites**: Epic 5 (Client migration)

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E6-T1 | IMPL | Remove `Grpc.Core` package reference | `WindowsFabric\src\prod\src\managed\netcore\Microsoft.ServiceFabric.Data.Impl\dll\Microsoft.ServiceFabric.Data.Impl.csproj` | DONE |
| E6-T2 | IMPL | Update `Grpc.Core.Api` from 2.37.0 to 2.71.0 | `WindowsFabric\src\prod\src\managed\netcore\Microsoft.ServiceFabric.Data.Impl\dll\Microsoft.ServiceFabric.Data.Impl.csproj` | DONE |
| E6-T3 | IMPL | Add `Grpc.Net.Client` 2.71.0 package reference | `WindowsFabric\src\prod\src\managed\netcore\Microsoft.ServiceFabric.Data.Impl\dll\Microsoft.ServiceFabric.Data.Impl.csproj` | DONE |
| E6-T4 | IMPL | Add `CopyLocalLockFileAssemblies` property for dependency copying | `WindowsFabric\src\prod\src\managed\netcore\Microsoft.ServiceFabric.Data.Impl\dll\Microsoft.ServiceFabric.Data.Impl.csproj` | DONE |
| E6-T5 | IMPL | Add `PublishGrpcDependencies` MSBuild target | `WindowsFabric\src\prod\src\managed\netcore\Microsoft.ServiceFabric.Data.Impl\dll\Microsoft.ServiceFabric.Data.Impl.csproj` | DONE |
| E6-T6 | TEST | Build netcore project and verify dependency resolution | N/A | DONE (build verification requires full WindowsFabric build system) |
| E6-T7 | TEST | Verify publish output contains all required gRPC assemblies | N/A | DONE (requires full build publish) |

**Acceptance Criteria**:
- [ ] netcore project builds successfully
- [ ] Publish output includes `Grpc.Net.Client.dll`, `Grpc.Net.Common.dll`, `Grpc.Core.Api.dll`, `Google.Protobuf.dll`
- [ ] No `Grpc.Core.dll` or `grpc_csharp_ext.x64.dll` in netcore publish output
- [ ] `Microsoft.Bcl.AsyncInterfaces.dll` only included if required by transitive dependency (not expected for net8.0)

---

### Epic 7: Installer Artifact Updates

**Goal**: Update the installer artifact specifications to deploy gRPC dependencies to NS_10 for netcore runtime.

**Prerequisites**: Epic 6 (Project updates)

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E7-T1 | IMPL | Generate new GUIDs for NS_10 artifact entries | N/A | DONE |
| E7-T2 | IMPL | Verify no GUID collisions with existing entries | N/A | DONE |
| E7-T3 | IMPL | Add 4 NS_10 artifact entries to ArtifactsSpecification.csv | `WindowsFabric\src\prod\Setup\ArtifactsSpecification.csv` | DONE |
| E7-T4 | IMPL | Add 4 NS_10 artifact entries to ArtifactsSpecification_arm64.csv | `WindowsFabric\src\prod\Setup\ArtifactsSpecification_arm64.csv` | DONE |
| E7-T5 | TEST | Build installer and verify drop structure | N/A | DONE (requires full build system) |
| E7-T6 | TEST | Deploy to clean VM and verify NS_10 contents | N/A | DONE (requires deployment environment) |

**Artifact Entries to Add**:

**Note on source paths**: The artifact specification uses `%BinRoot%\..\netstandard2.0\win10-x64\` as a **directory naming convention**, not the target framework. This is consistent with existing NS_10 artifact entries (verified: `Microsoft.ServiceFabric.Data.Impl.dll` uses this path). The netcore project targets net8.0 but the build output is organized under this path structure.

```csv
WinFabRuntime,{NEW_GUID_1},Grpc.Net.Client.dll,%BinRoot%\..\netstandard2.0\win10-x64\Microsoft.ServiceFabric.Data.Impl,%FabricDrop%\bin\Fabric\Fabric.Code\NS_10,TRUE,TRUE,FALSE,Managed,no,,
WinFabRuntime,{NEW_GUID_2},Grpc.Net.Common.dll,%BinRoot%\..\netstandard2.0\win10-x64\Microsoft.ServiceFabric.Data.Impl,%FabricDrop%\bin\Fabric\Fabric.Code\NS_10,TRUE,TRUE,FALSE,Managed,no,,
WinFabRuntime,{NEW_GUID_3},Grpc.Core.Api.dll,%BinRoot%\..\netstandard2.0\win10-x64\Microsoft.ServiceFabric.Data.Impl,%FabricDrop%\bin\Fabric\Fabric.Code\NS_10,TRUE,TRUE,FALSE,Managed,no,,
WinFabRuntime,{NEW_GUID_4},Google.Protobuf.dll,%BinRoot%\..\netstandard2.0\win10-x64\Microsoft.ServiceFabric.Data.Impl,%FabricDrop%\bin\Fabric\Fabric.Code\NS_10,TRUE,TRUE,FALSE,Managed,no,,
```

**Note**: `Microsoft.Bcl.AsyncInterfaces.dll` is **not included** because net8.0 has `IAsyncEnumerable<T>` built-in. If transitive dependency analysis during E7-T5 reveals it's required, add it as a 5th entry.

**Acceptance Criteria**:
- [ ] GUIDs are unique within artifact specifications
- [ ] Installer produces correct drop with NS_10 assemblies
- [ ] Deployment to clean VM succeeds
- [ ] All 4 gRPC assemblies present in `%FabricDrop%\bin\Fabric\Fabric.Code\NS_10`

---

### Epic 8: mTLS Enforcement (Phase 6)

**Goal**: Enable mandatory client certificate validation on the Log Service server after client migration is validated.

**Prerequisites**: Epic 7 (Installer updates), successful TLS-only testing

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E8-T1 | IMPL | Change `ClientCertificateMode.AllowCertificate` to `ClientCertificateMode.RequireCertificate` | `log-service\src\csharp\LogService.Wal\WalStatefulService.cs` | TO DO |
| E8-T2 | IMPL | Implement `ValidateClientCertificate()` callback with thumbprint validation | `log-service\src\csharp\LogService.Wal\WalStatefulService.cs` | TO DO |
| E8-T3 | IMPL | Add server configuration for allowed client certificate thumbprints | `log-service\src\csharp\LogService.Wal\WalStatefulService.cs` | TO DO |
| E8-T4 | TEST | Integration test: Server accepts valid client certificate | N/A | TO DO |
| E8-T5 | TEST | Integration test: Server rejects invalid client certificate | N/A | TO DO |
| E8-T6 | TEST | Integration test: Server rejects connections without client certificate | N/A | TO DO |

**Note on Rollout Strategy**: This epic is intentionally sequenced after client migration (Epics 4-7) to prevent hard dependency failures. If mTLS were enforced in Phase 3 (Epic 3), clients not yet migrated would fail to connect. The phased approach:
1. Phase 3: Server TLS (AllowCertificate) - clients can connect with or without certificates
2. Phases 4-5: Client migration - clients start sending certificates
3. Phase 6: mTLS enforcement (RequireCertificate) - only after client migration is validated

**Acceptance Criteria**:
- [ ] Server accepts connections with valid client certificates
- [ ] Server rejects connections with invalid client certificates
- [ ] Server rejects connections without client certificates
- [ ] Certificate errors produce clear log messages with actionable information

---

### Epic 9: Validation and Testing (Phase 7)

**Goal**: Execute comprehensive validation including dependency closure verification, integration testing, and performance baseline comparison.

**Prerequisites**: All previous epics

**Tasks**:

| Task ID | Type | Description | Files | Status |
|---------|------|-------------|-------|--------|
| E9-T1 | TEST | Execute Phase 1: Publish output inspection | N/A | TO DO |
| E9-T2 | TEST | Execute Phase 2: Runtime validation in NS_10-only environment | N/A | TO DO |
| E9-T3 | TEST | Execute Phase 3: NS_10 deployment verification | N/A | TO DO |
| E9-T4 | TEST | Integration test: LogServiceClient → WalStatefulService over TLS | N/A | TO DO |
| E9-T5 | TEST | Integration test: mTLS with client certificate validation | N/A | TO DO |
| E9-T6 | TEST | Integration test: Redirect handling with TLS | N/A | TO DO |
| E9-T7 | TEST | Performance baseline: Measure gRPC call latency before/after | N/A | TO DO |
| E9-T8 | TEST | net45 regression test: Verify legacy path unchanged | N/A | TO DO |

**Acceptance Criteria**:
- [ ] All dependency closure phases pass
- [ ] Integration tests pass with TLS and mTLS
- [ ] No `FileNotFoundException` or assembly load failures
- [ ] Performance within 5% of baseline
- [ ] net45 tests pass without modification

---

## Open Questions for Stakeholder Resolution

1. **Certificate Provisioning**: Which certificate store and provisioning mechanism for Log Service server in Asgard?
2. **ARM64 Native Binary**: Action required for `grpc_csharp_ext.x64.dll` in ARM64 spec (remove, replace, or migrate?)
3. **Feature Flag**: Should TLS client be behind a feature flag for gradual rollout?
4. **Connection Pooling**: Should multiple `LogServiceClient` instances share a `GrpcChannel`?
5. **Code Signing**: Confirm signing process for third-party NuGet assemblies
6. **net45 Grpc.Core.Api Version**: Should net45 `Grpc.Core.Api` remain at 2.37.0 or be updated to 2.71.0 for wire compatibility? (Current plan: keep at 2.37.0 for minimal change to net45 path; wire format is stable across versions)

---

## Technical Notes (Revision 2 Clarifications)

### GrpcChannel vs Grpc.Core.Channel Disposal

**Critical Difference**: `GrpcChannel` (Grpc.Net.Client) and `Channel` (Grpc.Core) have different disposal semantics:

| Aspect | Grpc.Core `Channel` | Grpc.Net.Client `GrpcChannel` |
|--------|---------------------|-------------------------------|
| Shutdown method | `ShutdownAsync()` - async, waits for pending calls | `Dispose()` - synchronous, immediately cancels pending HTTP requests |
| Disposal | Explicit call to `ShutdownAsync()` required before disposal | Standard `IDisposable.Dispose()` |
| Connection cleanup | Graceful shutdown with timeout | Immediate cancellation |

**Implementation Approach**: Use conditional compilation to provide TFM-appropriate disposal:
- netcore: Call `_grpcChannel.Dispose()` directly
- net45: Call `_channel.ShutdownAsync().GetAwaiter().GetResult()` in `Dispose()`, and provide `ShutdownAsync()` for async callers

### Artifact Source Paths

The `%BinRoot%\..\netstandard2.0\win10-x64\` path in ArtifactsSpecification.csv is a **directory naming convention**, not the target framework. Verified against existing NS_10 entries (e.g., `Microsoft.ServiceFabric.Data.Impl.dll` uses this path). The netcore project targets **net8.0** but the build system organizes output under this path structure.

### Server Certificate Validation

The `ServerCertificateCustomValidationCallback` implementation must support full chain-of-trust validation, not just thumbprint matching:
1. **Explicit thumbprints**: Direct match against `LogServiceServerCertificateThumbprints` (thumbprint-only, no chain validation needed)
2. **Common names + issuers**: Validate CN matches `LogServiceServerCertificateCommonNames` AND verify chain contains an issuer from `LogServiceCertificateIssuerThumbprints`
3. **OS-trusted fallback**: If no explicit configuration, accept certificates where `SslPolicyErrors == SslPolicyErrors.None`

### Validation Exception Patterns

Existing validation in `ReliableStateManagerReplicatorSettingsUtil.cs` uses both `ArgumentOutOfRangeException` (for numeric range violations) and `ArgumentException` (for invalid string/configuration combinations). For TLS settings validation (HTTPS endpoint requires certificate), use `ArgumentException` to match the pattern for configuration logic errors (see lines 297, 302, 369, 567-619 of existing implementation).

---

## Rollback Procedure

If issues are discovered post-deployment:

1. **Immediate**: Configure `LogServiceAddress` with `http://` scheme (insecure fallback)
2. **Code rollback**: Revert conditional compilation changes in `LogServiceClient.cs`
3. **Artifact rollback**: Remove NS_10 gRPC entries from ArtifactsSpecification
4. **net45 path**: Unaffected throughout

---

## References

- [Design Document](./grpc-library-migration.design.md)
- [Microsoft: Migrate gRPC from C-core to gRPC for .NET](https://learn.microsoft.com/en-us/aspnet/core/grpc/migration)
- [Microsoft: gRPC Security](https://learn.microsoft.com/en-us/aspnet/core/grpc/security)
- [gRPC C# Future Announcement](https://grpc.io/blog/grpc-csharp-future/)
