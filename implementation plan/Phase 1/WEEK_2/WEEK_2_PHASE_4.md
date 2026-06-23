Continue Week 2 Phase 4 - Implement vendor abstraction and Priva API stub for Phase 1.5 integration.

Phase 4 (after Phase 3):

- backend-engineer: Implement vendor adapter classes including CSV and Priva stub.
  Output: src/vendors/__init__.py, src/vendors/base.py, src/vendors/csv_adapter.py, src/vendors/priva_client.py
  
  Deliverables:
  
  1. src/vendors/base.py
     Define abstract vendor interface:
     
     - from abc import ABC, abstractmethod
     
     - class DataSourceAdapter(ABC):
       
       @abstractmethod
       async def fetch_data(self, tenant_id: str, start: datetime, end: datetime) -> np.ndarray:
         """
         Fetch sensor data from source.
         
         Args:
           tenant_id: Customer identifier
           start: Start timestamp
           end: End timestamp
         
         Returns:
           np.ndarray shape (N, seq_len, 4) - normalized [0, 1]
         """
       
       @abstractmethod
       def validate_data(self, data: np.ndarray) -> Tuple[bool, str]:
         """
         Validate data from this vendor.
         
         Returns:
           (is_valid: bool, error_message: str)
         """
       
       @abstractmethod
       async def normalize(self, data: np.ndarray) -> np.ndarray:
         """
         Normalize vendor-specific data format to [0, 1].
         """
       
       @property
       @abstractmethod
       def name(self) -> str:
         """Vendor name (csv, priva, etc.)"""
  
  2. src/vendors/csv_adapter.py
     Implement CSV data source adapter:
     
     - class CSVAdapter(DataSourceAdapter):
       
       async def fetch_data(tenant_id: str, file: UploadFile) -> np.ndarray:
         """
         Parse CSV file and return sequences.
         
         Expected CSV columns:
         - timestamp (ISO format)
         - temperature (float, °C)
         - humidity (float, %)
         - co2 (float, ppm)
         - light (float, lux)
         """
       
       def validate_data(data: np.ndarray) -> Tuple[bool, str]:
         """Check for NaN, Inf, out-of-range values"""
       
       async def normalize(data: np.ndarray) -> np.ndarray:
         """Normalize using config ranges (from src/config.py)"""
       
       @property
       def name(self) -> str:
         return "csv"
  
  3. src/vendors/priva_client.py
     Create Priva API client stub (ready for Phase 1.5):
     
     - class PrivaClient(DataSourceAdapter):
       
       def __init__(self, api_key: str, base_url: str = "https://api.priva.com"):
         """
         Initialize Priva API client (stub).
         
         Phase 1.5: Replace with real Priva API calls.
         """
         self.api_key = api_key
         self.base_url = base_url
       
       async def fetch_data(self, tenant_id: str, greenhouse_id: str, 
                           start: datetime, end: datetime) -> np.ndarray:
         """
         Fetch sensor data from Priva API.
         
         Phase 1: Returns mock data
         Phase 1.5: Will call real Priva endpoints:
           - GET /api/v1/greenhouse/{greenhouse_id}/sensors
           - Filter by timestamp range
           - Parse Priva's response format
         
         Mock response for testing:
           Returns simulated sensor data with same shape as CSV
         """
         # Phase 1: Return mock data
         # Phase 1.5: Replace with real API calls
         mock_sequences = np.random.rand(100, 20, 4)  # Mock: 100 sequences
         return await self.normalize(mock_sequences)
       
       def validate_data(data: np.ndarray) -> Tuple[bool, str]:
         """Validate Priva-specific data format"""
       
       async def normalize(data: np.ndarray) -> np.ndarray:
         """
         Normalize Priva's sensor ranges to [0, 1].
         
         Priva sensors may have different ranges than CSV.
         This adapter handles the conversion.
         """
       
       @property
       def name(self) -> str:
         return "priva"
       
       # Methods for Phase 1.5 (scaffolding):
       async def _authenticate(self) -> str:
         """Get OAuth token from Priva (stub)"""
         pass
       
       async def _list_greenhouses(self) -> List[str]:
         """List greenhouses for this customer (stub)"""
         pass
       
       async def _get_sensor_data(self, greenhouse_id: str, start: datetime, 
                                 end: datetime) -> Dict:
         """Get sensor readings from Priva API (stub)"""
         pass
  
  4. src/vendors/__init__.py
     Export adapters:
     
     from .base import DataSourceAdapter
     from .csv_adapter import CSVAdapter
     from .priva_client import PrivaClient
     
     __all__ = ["DataSourceAdapter", "CSVAdapter", "PrivaClient"]
  
  Architecture notes:
  - Each vendor implements the same interface (DataSourceAdapter)
  - Easy to add new vendors: just create new class
  - Used in Phase 1.5: switch between CSV and Priva with single config change
  - Testing: use mock implementations

- qa-engineer: Write tests for vendor adapters.
  Output: tests/vendors/test_csv_adapter.py, tests/vendors/test_priva_stub.py
  
  Deliverables:
  
  1. tests/vendors/test_csv_adapter.py
     Test CSV adapter functionality:
     
     - test_csv_adapter_init
       Create adapter, check name is "csv"
     
     - test_parse_valid_csv
       Load valid CSV file, check output shape (N, 20, 4)
     
     - test_validate_valid_data
       Data with all values in range → returns (True, "")
     
     - test_validate_nan_values
       Data with NaN → returns (False, "NaN detected")
     
     - test_validate_out_of_range
       Temperature = 100°C → returns (False, "Temperature out of range")
     
     - test_normalize_data
       Raw data [0, 1000] → normalized [0, 1]
     
     - test_csv_parse_invalid_format
       Missing columns → raises ValueError
     
     - test_csv_large_file
       Parse 10,000 rows → successful, correct shape
  
  2. tests/vendors/test_priva_stub.py
     Test Priva stub (mock implementation):
     
     - test_priva_client_init
       Create client with API key and base URL
     
     - test_priva_mock_fetch_data
       Call fetch_data() → returns mock data shape (100, 20, 4)
     
     - test_priva_normalize
       Mock data normalized to [0, 1]
     
     - test_priva_validate_data
       Mock data passes validation
     
     - test_priva_authentication_stub
       _authenticate() method exists, returns placeholder
     
     - test_priva_list_greenhouses_stub
       _list_greenhouses() method exists, returns placeholder
     
     - test_priva_get_sensor_data_stub
       _get_sensor_data() method exists, returns placeholder
     
     - test_interface_compliance
       PrivaClient implements DataSourceAdapter interface
       Has all required methods: fetch_data, validate_data, normalize, name
  
  3. tests/vendors/__init__.py (empty)
  
  Fixtures:
  - @pytest.fixture
    def sample_csv_file(tmp_path):
      Create valid CSV for testing
    
  - @pytest.fixture
    def csv_adapter():
      Return CSVAdapter instance
    
  - @pytest.fixture
    def priva_client():
      Return PrivaClient instance (stub)

auto-proceed